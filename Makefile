# AI Gateway NAS operations Makefile
#
# Run these from the repo root on the Docker host / NAS.
# Commands delegate to the src/ directory where docker-compose.yml lives.

SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
MAKEFLAGS += --no-print-directory

COMPOSE_DIR := src
COMPOSE := docker compose -f $(COMPOSE_DIR)/docker-compose.yml

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

.PHONY: regen
regen: ## Regenerate runtime configs from src/gateway.config.yaml
	python3 $(COMPOSE_DIR)/scripts/generate_configs.py

.PHONY: regen-opencode
regen-opencode: ## Sync the gateway catalog into ~/.config/opencode/opencode.json
	python3 $(COMPOSE_DIR)/scripts/generate_opencode_config.py

.PHONY: regen-all
regen-all: regen regen-opencode ## Regenerate configs and sync opencode models

.PHONY: redeploy
redeploy: ## Build and (re)start the full stack
	$(COMPOSE) up -d --build

.PHONY: regen-redeploy
regen-redeploy: regen redeploy ## Regenerate configs, then redeploy the full stack

.PHONY: update
update: ## Pull fresh images and redeploy the full stack
	$(COMPOSE) pull
	$(COMPOSE) up -d --build

.PHONY: restart-router
restart-router: ## Restart only the sticky router (after router code/config changes)
	$(COMPOSE) restart sticky-router

.PHONY: restart-litellm
restart-litellm: ## Restart only LiteLLM (after provider secrets/model config changes)
	$(COMPOSE) restart litellm

.PHONY: health
health: ## Show compose status and hit healthz/readyz
	@echo "=== Service status ==="
	$(COMPOSE) ps
	@echo ""
	@echo "=== healthz ==="
	curl -fsS http://localhost:4100/healthz | python3 -m json.tool || true
	@echo ""
	@echo "=== readyz ==="
	curl -fsS http://localhost:4100/readyz | python3 -m json.tool || true

.PHONY: logs
logs: ## Tail sticky-router and litellm logs
	$(COMPOSE) logs -f sticky-router litellm

.PHONY: smoke
smoke: ## Smoke test with VIRTUAL_KEY env var
	@if [ -z "$${VIRTUAL_KEY:-}" ]; then \
		echo "Error: set VIRTUAL_KEY to a LiteLLM virtual key before running make smoke" >&2; \
		exit 1; \
	fi
	curl -fsS http://localhost:4100/v1/chat/completions \
		-H "Authorization: Bearer $${VIRTUAL_KEY}" \
		-H "Content-Type: application/json" \
		-H "X-Session-Id: make-smoke-test" \
		-d '{"messages":[{"role":"user","content":"say OK only"}],"max_tokens":80}'
	@echo

.PHONY: models
models: ## List available models (requires VIRTUAL_KEY)
	@if [ -z "$${VIRTUAL_KEY:-}" ]; then \
		echo "Error: set VIRTUAL_KEY to a LiteLLM virtual key before running make models" >&2; \
		exit 1; \
	fi
	curl -fsS http://localhost:4100/v1/models \
		-H "Authorization: Bearer $${VIRTUAL_KEY}"
	@echo

.PHONY: backup
backup: ## Backup Postgres, Redis AOF, and config files into backups/
	@mkdir -p backups
	@echo "=== Backing up Postgres ==="
	$(COMPOSE) exec postgres pg_dump -U "$${POSTGRES_USER}" "$${POSTGRES_DB}" \
		> backups/postgres-$$(date +%F).sql
	@echo "=== Backing up Redis AOF ==="
	$(COMPOSE) exec redis redis-cli -a "$${REDIS_PASSWORD}" BGSAVE
	$(COMPOSE) cp redis:/data/appendonly.aof backups/redis-$$(date +%F).aof
	@echo "=== Backing up config files ==="
	tar czf backups/ai-gateway-config-$$(date +%F).tgz \
		-C $(COMPOSE_DIR) \
		docker-compose.yml gateway.config.yaml litellm.config.yaml router/router_config.yaml
	@echo "=== Backup complete in backups/ ==="

.PHONY: stop
stop: ## Stop the stack without removing containers/volumes
	$(COMPOSE) stop

.PHONY: down
down: ## Stop and remove containers (keeps named volumes by default)
	$(COMPOSE) down

.PHONY: down-volumes
down-volumes: ## WARNING: stop and remove containers AND named volumes
	@echo "This will delete Postgres data, Redis data, virtual keys, and spend history."
	@read -p "Type 'delete-everything' to continue: " confirm && [ "$$confirm" = "delete-everything" ]
	$(COMPOSE) down -v

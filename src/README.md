# Synology AI Gateway

This runs a small personal AI gateway stack with Docker:

- Sticky FastAPI router on port `4100`
- LiteLLM proxy on internal port `4000`
- Postgres for LiteLLM virtual keys, spend tracking, and UI state
- Redis for LiteLLM cache state and router session stickiness

Agents talk to the router. The router chooses an alias, keeps warm sessions on
the same alias, redacts obvious secrets, and forwards the request to LiteLLM.
LiteLLM handles provider adapters, virtual keys, caching, fallbacks, and spend
tracking.

## Files

```text
src/
  docker-compose.yml
  gateway.config.yaml
  litellm.config.yaml
  .env.example
  README.md
  scripts/
    generate_configs.py
  router/
    Dockerfile
    requirements.txt
    main.py
    redaction.py
    routing.py
    router_config.yaml
    sessions.py
    tests/
```

## Setup

On the NAS, copy this folder somewhere persistent, for example:

```bash
/volume1/docker/ai-gateway
```

Create the secret env file:

```bash
cd /volume1/docker/ai-gateway
cp .env.example .env
chmod 600 .env
```

Edit `.env` and set:

- `LITELLM_MASTER_KEY`
- `LITELLM_SALT_KEY`
- `POSTGRES_PASSWORD`
- `DATABASE_URL` with the same Postgres password
- `REDIS_PASSWORD`
- `REDIS_URL` with the same Redis password
- `DEEPSEEK_API_KEY`
- `OPENCODE_GO_API_KEY`
- `OLLAMA_API_KEY`
- `OLLAMA_API_BASE=https://ollama.com` for Ollama Cloud

Start it:

```bash
docker compose up -d --build
docker compose logs -f sticky-router litellm
```

## Agent Endpoint

The OpenAI-compatible endpoint for agents is:

```text
http://<nas-host>:4100/v1
```

Health check:

```bash
curl http://localhost:4100/healthz
```

Model discovery (use a virtual key, not the master key, for agents):

```bash
curl http://localhost:4100/v1/models \
  -H "Authorization: Bearer $VIRTUAL_KEY"
```

Chat smoke test (virtual key):

```bash
curl http://localhost:4100/v1/chat/completions \
  -H "Authorization: Bearer $VIRTUAL_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Session-Id: smoke-test" \
  -d '{
    "messages": [{"role": "user", "content": "say OK only"}],
    "max_tokens": 80
  }'
```

Admin smoke test (master key, for setup/verification only):

```bash
curl http://localhost:4100/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Session-Id: admin-smoke" \
  -d '{
    "messages": [{"role": "user", "content": "say OK only"}],
    "max_tokens": 80
  }'
```

The router adds these response headers:

- `X-Gateway-Model`: selected LiteLLM alias
- `X-Gateway-Reason`: `explicit-model`, `warm-session`, or `default-model`
- `X-Gateway-Fallback-Count`: number of fallback hops (0 when no fallback)
- `X-Gateway-Fallback-From`: original alias, only present when a fallback occurred

Health and readiness:

```bash
curl http://localhost:4100/healthz   # liveness, always 200, per-dep status
curl http://localhost:4100/readyz    # readiness, 503 if any dep is down
```

Router metrics:

```bash
curl http://localhost:4100/metrics | python3 -m json.tool
```

The metrics endpoint is an in-memory debug view that resets when the router
container restarts. It reports request totals, selected and served aliases,
fallback count, LiteLLM cache counts (`hit`, `miss`, `unknown`), and
per-alias upstream availability. Availability is keyed by LiteLLM alias and
counts every upstream attempt, including failed attempts before a fallback:

```json
{
  "provider_availability": {
    "opencodego-fast": {
      "attempts": 12,
      "successes": 10,
      "failures": 2,
      "retryable_failures": 2,
      "availability_percent": 83.33,
      "last_status": 503,
      "last_failure_ts": 1782820800.0
    }
  }
}
```

## Virtual Keys And Model Allowlists

Agents should use LiteLLM virtual keys, not the master key. Create one key
per agent/tool with a model allowlist so a background summarizer cannot use
an expensive alias.

### Per-Agent Keys

The following virtual keys are provisioned on a fresh stack (re-create them
after a `docker compose down -v` that wipes Postgres):

| Agent / Tool   | Key alias      | Allowlist                                         | Max budget |
| -------------- | -------------- | ------------------------------------------------- | ---------- |
| Codex CLI      | `codex-cli`    | `opencodego-fast`, `opencodego-code`, `fast`, `deepseek-pro` | $5.00      |
| Summarizer     | `summarizer`   | `fast`, `ollama-cloud`                            | $2.00      |

The `summarizer` key is intentionally restricted to the cheaper aliases so a
background summarizer cannot spend on `deepseek-pro` or `opencodego-*`.

### Creating A Virtual Key

Create a virtual key (admin only, with the master key):

```bash
docker compose exec litellm python3 -c "
import os, json, urllib.request
body = json.dumps({
    'key_alias': 'codex-cli',
    'models': ['opencodego-fast', 'opencodego-code', 'fast', 'deepseek-pro'],
    'max_budget': 5.0
}).encode()
req = urllib.request.Request(
    'http://localhost:4000/key/generate',
    data=body,
    headers={
        'Authorization': 'Bearer ' + os.environ['LITELLM_MASTER_KEY'],
        'Content-Type': 'application/json',
    },
    method='POST',
)
print(json.load(urllib.request.urlopen(req, timeout=30))['key'])
"
```

Use the returned `key` value as the `Authorization: Bearer <key>` for that
agent. Replace `reasoning` with an alias you have configured if it is not in
`gateway.config.yaml`.

The master key is only for admin setup (creating keys, viewing spend). Do not
put it in agent configs.

## Codex CLI Config

Point Codex at the gateway with a custom model provider or
`OPENAI_BASE_URL`:

```bash
export OPENAI_BASE_URL=http://<nas-host>:4100/v1
export OPENAI_API_KEY=<litellm-virtual-key>
```

Codex Pro is a client entitlement; it calls this gateway as a client and is
not configured as an upstream provider.

## Active Aliases

Use model aliases from `gateway.config.yaml`:

- `fast`: DeepSeek V4 Flash direct API
- `deepseek-pro`: DeepSeek V4 Pro direct API
- `opencodego-fast`: OpenCode Go `kimi-k2.7-code`
- `opencodego-code`: OpenCode Go `deepseek-v4-pro`
- `ollama-cloud`: Ollama Cloud `gemma3:27b`

Copilot Pro is intentionally skipped for now. Codex Pro is a client that can
call this gateway; it is not configured as an upstream provider.

## Routing

The router chooses aliases with deterministic rules:

- explicit allowed `model` field wins
- warm `X-Session-Id` keeps the previous model until the Redis TTL expires
- otherwise, the configured `default_model` is used

Default session TTL is 600 seconds.

## Synology Container Manager

In Container Manager:

1. Create a Project.
2. Point it at this folder.
3. Use `docker-compose.yml`.
4. Create `.env` before first deploy.
5. Deploy the project.

If Container Manager creates empty folders for missing mounted files, stop the
project, create the real files, and redeploy.

## Network Exposure

Do not expose the gateway directly to the public internet.

Preferred access:

- Tailscale on the NAS, then use `http://<tailscale-ip>:4100/v1`.
- Cloudflare Tunnel with Access in front of it.
- WireGuard/VPN to your home network.

For local LAN only, use:

```text
http://<nas-lan-ip>:4100/v1
```

LiteLLM port `4000`, Postgres `5432`, and Redis `6379` are internal Docker
ports. The LiteLLM admin UI is not exposed by default.

## Updating

```bash
cd /volume1/docker/ai-gateway
docker compose pull
docker compose up -d --build
```

## Daily Spend Summary

LiteLLM tracks spend when the proxy is backed by the configured database. For
a one-day summary, run the report from inside the LiteLLM container so port
`4000` stays internal:

```bash
cd /volume1/docker/ai-gateway

docker compose exec -T litellm python3 - <<'PY'
import datetime as dt
import json
import os
import urllib.request

today = dt.datetime.now(dt.timezone.utc).date()
start = today - dt.timedelta(days=1)
url = (
    "http://localhost:4000/user/daily/activity"
    f"?start_date={start.isoformat()}&end_date={today.isoformat()}"
)
request = urllib.request.Request(
    url,
    headers={"Authorization": f"Bearer {os.environ['LITELLM_MASTER_KEY']}"},
)
with urllib.request.urlopen(request, timeout=30) as response:
    print(json.dumps(json.load(response), indent=2))
PY
```

Use the LiteLLM admin UI only for manual inspection. The compose file does not
publish LiteLLM port `4000`, so there is no always-on admin UI maintenance
profile to protect. If the UI becomes necessary often enough, add a temporary
Tailscale-only profile later; until then prefer admin API commands from inside
the container.

## Backup And Restore

Back up the two named volumes and the config folder before any upgrade.

Backup (run on the NAS):

```bash
cd /volume1/docker/ai-gateway

# Postgres: logical dump
docker compose exec postgres pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" \
  > backups/postgres-$(date +%F).sql

# Redis: append-only file snapshot
docker compose exec redis redis-cli -a "$REDIS_PASSWORD" BGSAVE
docker compose cp redis:/data/appendonly.aof backups/redis-$(date +%F).aof

# Config + secrets (to a safe, offline location)
tar czf backups/ai-gateway-config-$(date +%F).tgz \
  docker-compose.yml gateway.config.yaml litellm.config.yaml router/router_config.yaml .env
```

Restore:

```bash
cd /volume1/docker/ai-gateway
docker compose down

# Postgres
docker compose up -d postgres
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  < backups/postgres-YYYY-MM-DD.sql

# Redis
docker cp backups/redis-YYYY-MM-DD.aof ai-gateway-redis:/data/appendonly.aof
docker compose restart redis

docker compose up -d --build
```

Keep `backups/` outside the project folder or add it to `.gitignore`.

## Rollback

If an upgrade breaks the gateway:

```bash
cd /volume1/docker/ai-gateway
docker compose down
# Restore the previous config + .env from backup, then:
git checkout <previous-tag>   # if the repo lives on the NAS
docker compose up -d --build
# If Postgres schema changed, restore from the pre-upgrade SQL dump.
```

## Secret Rotation

Rotate provider keys and the LiteLLM master/salt keys without losing state:

1. Generate new secrets (see `.env` generation helper below).
2. Edit `.env` in place with the new values.
3. For `LITELLM_MASTER_KEY` and `LITELLM_SALT_KEY`: restart LiteLLM only
   (`docker compose restart litellm`). Existing virtual keys and stored
   provider credentials are encrypted with `SALT_KEY`; rotating it requires
   re-creating keys and re-adding provider credentials via the LiteLLM admin
   UI. Do this in a maintenance window.
4. For provider API keys (`DEEPSEEK_API_KEY`, etc.): edit `.env`, then
   `docker compose restart litellm`.
5. Verify with a smoke test using a virtual key.

## .env Generation Helper

Generate strong random secrets for `.env`:

```bash
python3 - <<'PY'
import secrets
keys = [
    "LITELLM_MASTER_KEY", "LITELLM_SALT_KEY", "POSTGRES_PASSWORD",
    "REDIS_PASSWORD",
]
for k in keys:
    print(f"{k}=sk-{secrets.token_urlsafe(32)}")
PY
```

Provider API keys (`DEEPSEEK_API_KEY`, `OPENCODE_GO_API_KEY`, `OLLAMA_API_KEY`)
come from the provider dashboards and must be pasted in manually. The helper
only generates the internal secrets.

## Tailscale-Only Exposure

The default posture is Tailscale-only. The gateway should never be exposed
directly to the public internet.

- Install Tailscale on the NAS and join the tailnet.
- Agents use `http://<nas-tailscale-ip>:4100/v1`.
- Only port `4100` is published by the router service; `4000`, `5432`, and
  `6379` stay on the internal Docker network.
- If you need remote access without Tailscale, use a Cloudflare Tunnel with
  Access policies in front of it — never a raw port forward.

## Local Tests

```bash
python3 -m pip install -r router/requirements-dev.txt
PYTHONPATH=. python3 -m pytest router/tests -q
```

Back up the Docker volumes before major upgrades:

- `ai-gateway_postgres_data`
- `ai-gateway_redis_data`

## Notes

- `LITELLM_SALT_KEY` is used to encrypt stored provider credentials. Do not
  rotate it casually after creating models or keys.
- Keep `.env` only on the NAS or in a password manager.
- Put provider model changes in `gateway.config.yaml`, run
  `python3 scripts/generate_configs.py`, then redeploy.
- LiteLLM can warn that custom model costs are missing. That does not block
  calls; add `model_info` pricing later if exact spend reporting matters.

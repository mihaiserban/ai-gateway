# Project agent instructions

## Completion review

After making code changes and before declaring a task complete, invoke the **`ponytail-review`** skill on the resulting diff to find over-engineering, bloat, and unnecessary complexity. Apply its simplification suggestions unless they directly conflict with explicit requirements, tests, or project constraints.

After completing a task, update any documentation that is affected by the change. This includes README files, docs, examples, configuration references, and developer-facing instructions. If no documentation changes are needed, note that explicitly in the completion summary.

For branch- or project-level work, also run **`ponytail-audit`** on the whole repository before merging or opening a pull request.

## Running and testing locally

Dev setup and commands are documented in [README.md § Local Development](README.md#local-development). The usual commands for this repo are:

```bash
python3 -m venv .venv && . .venv/bin/activate
python3 -m pip install -r src/router/requirements-dev.txt
python3 -m pytest -q                 # or `python3 -m pytest --cov -q` for the 80% coverage gate
python3 -m ruff check .
python3 -m ruff format --check .
python3 -m mypy
```

The human-edited config is `src/gateway.config.yaml`. After changing it, regenerate runtime YAML:

```bash
python3 src/scripts/generate_configs.py
```

Generated files such as `src/litellm.config.yaml` and `src/router/router_config.yaml` are committed for the Docker stack, but they are not the edit point.

## Redeploying the stack to test

The Docker Compose file lives in `src/`. On the NAS the project is typically checked out under `/volume1/docker/ai-gateway`; on a dev machine use the repo's `src/` directory. Run `docker compose` from whichever directory holds the active `.env`.

Full rebuild after router or LiteLLM config/code changes:

```bash
python3 src/scripts/generate_configs.py   # only if gateway.config.yaml changed
docker compose up -d --build
docker compose logs -f sticky-router litellm
```

Targeted restarts are faster when only one service changed:

- `docker compose restart sticky-router` — after router code or `src/router/router_config.yaml` changes.
- `docker compose restart litellm` — after provider secrets, `src/litellm.config.yaml`, or `gateway.config.yaml` changes.

Verify status:

```bash
docker compose ps
curl http://localhost:4100/healthz
curl http://localhost:4100/readyz
```

Smoke test with a virtual key (not the master key):

```bash
curl http://localhost:4100/v1/chat/completions \
  -H "Authorization: Bearer $VIRTUAL_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Session-Id: smoke-test" \
  -d '{"messages":[{"role":"user","content":"say OK only"}],"max_tokens":80}'
```

Avoid `docker compose down -v` unless you intentionally want to wipe virtual keys, spend history, and Redis cache/session data.

See also the runbook in [src/README.md](src/README.md) and [README.md § Operations](README.md#operations).

## Using this gateway as a client

OpenAI-compatible clients should point at `http://<host>:4100/v1`.

Use a LiteLLM virtual key per agent/tool; never give agents the master key. Configure per-agent model allowlists and budgets in [src/README.md § Virtual Keys](src/README.md#virtual-keys-and-model-allowlists).

Prefer task aliases when selecting a model: `explorer`, `planner`, `coder`, `coder-fast`, `vision`. Use model-family aliases when you need a specific model with provider fallback; use provider-deployment aliases only to force one backend. Full table and guidance are in [src/README.md § Active Aliases](src/README.md#active-aliases).

For opencode, sync the gateway catalog into `~/.config/opencode/opencode.json`:

```bash
python3 src/scripts/generate_opencode_config.py
```

Use `--dry-run` to preview the merged model list without writing the file.

Send `X-Session-Id` to keep a conversation on the same model within the Redis TTL.

# agent-ai-gateway

Personal OpenAI-compatible AI gateway for local agents. The stack exposes one
stable endpoint, routes requests to cheaper or stronger model aliases, keeps
warm conversations on the same model, redacts obvious secrets before provider
egress, and delegates provider adapters, virtual keys, caching, budgets, and
spend tracking to LiteLLM.

The deployment target is a small Docker stack on a Synology NAS or another
always-on LAN/Tailscale host.

## What It Runs

```text
agents
  |
  | OpenAI-compatible API
  v
sticky-router (:4100, FastAPI)
  |
  | internal Docker network
  v
LiteLLM (:4000)
  |
  +--> DeepSeek
  +--> OpenCode Go
  +--> Ollama Cloud/local
  +--> future providers

Redis     -> LiteLLM cache state and router session stickiness
Postgres  -> LiteLLM virtual keys, budgets, spend, and UI state
```

Only the router publishes a host port by default, and it is bound to
`127.0.0.1:4100` so it is local to the Docker host. LiteLLM, Postgres, and
Redis stay internal to Docker.

## Repository Layout

```text
.
+-- README.md                  # project overview and usage
+-- docs/                      # planning, status notes, runbooks, reports
+-- pyproject.toml             # lint, type-check, test, and coverage config
+-- src/
    +-- docker-compose.yml     # runtime stack
    +-- gateway.config.yaml    # human-edited model, routing, and LiteLLM values
    +-- litellm.config.yaml    # generated LiteLLM model aliases and cache settings
    +-- .env.example           # secret/config template
    +-- README.md              # NAS-oriented operations runbook
    +-- scripts/
        +-- generate_configs.py # regenerates runtime YAML from gateway config
    +-- router/
        +-- main.py            # FastAPI sticky router
        +-- routing.py         # routing and fallback decisions
        +-- redaction.py       # outbound prompt secret redaction
        +-- sessions.py        # Redis or memory session store
        +-- health.py          # dependency health checks
        +-- metrics.py         # in-memory debug metrics
        +-- router_config.yaml # generated router aliases, TTLs, timeouts, fallbacks
        +-- tests/             # pytest suite
```

## Runtime Services

| Service | Container | Purpose |
| --- | --- | --- |
| Sticky router | `ai-gateway-router` | Public OpenAI-compatible edge on port `4100`; handles model selection, session stickiness, redaction, fallback attempts, streaming passthrough, health, and debug metrics. |
| LiteLLM | `ai-gateway-litellm` | Provider adapter layer, virtual keys, model allowlists, spend tracking, Redis-backed cache, and provider-level settings. |
| Postgres | `ai-gateway-postgres` | LiteLLM durable state for virtual keys, budgets, spend, and UI metadata. |
| Redis | `ai-gateway-redis` | LiteLLM cache state plus router sticky-session state. |

## Supported API Surface

The router intentionally implements a small OpenAI-compatible surface:

| Endpoint | Method | Behavior |
| --- | --- | --- |
| `/v1/chat/completions` | `POST` | Honors the requested model alias or uses the configured default, redacts prompt secrets, forwards to LiteLLM, supports non-streaming and SSE streaming responses, and retries configured fallback aliases for retryable upstream failures. |
| `/v1/models` | `GET` | Proxies model discovery to LiteLLM. |
| `/healthz` | `GET` | Liveness-style health; returns HTTP `200` with per-dependency status. |
| `/readyz` | `GET` | Readiness; returns HTTP `503` when an enabled dependency is degraded. |
| `/metrics` | `GET` | In-memory debug counters for routing, fallbacks, cache observations, and provider availability. |
| `/v1/{anything-else}` | mixed | Returns HTTP `501` until explicitly implemented. |

## Compatibility Contract

The router is tested for OpenAI-compatible chat, streaming chat, and model
discovery behavior. It forwards `Authorization`, JSON content type, and the
request body to LiteLLM for supported paths. Unsupported `/v1/*` paths return
HTTP `501` and are not proxied.

| Client behavior | Path | Status |
| --- | --- | --- |
| Chat completions | `/v1/chat/completions` | Supported |
| Streaming chat completions | `/v1/chat/completions` with `stream: true` | Supported |
| Model discovery | `/v1/models` | Supported |
| Responses API | `/v1/responses` | Explicit `501` |
| Embeddings | `/v1/embeddings` | Explicit `501` |
| Images | `/v1/images/*` | Explicit `501` |
| Files | `/v1/files` | Explicit `501` |

Successful chat responses include gateway routing headers:

| Header | Meaning |
| --- | --- |
| `X-Gateway-Model` | Alias actually served by LiteLLM after fallback, if any. |
| `X-Gateway-Provider-Model` | Resolved provider/model string (e.g. `openai/kimi-k2.7-code`). |
| `X-Gateway-Reason` | `explicit-model`, `warm-session`, or `default-model`. |
| `X-Gateway-Fallback-Count` | Number of router fallback hops. |
| `X-Gateway-Fallback-From` | Original selected alias, present only after fallback. |

LiteLLM cache headers such as `x-litellm-cache-hit` and
`x-litellm-cache-key` are preserved when upstream returns them.

## Live Operations Dashboard

The router serves a read-only dashboard at:

```text
http://<host>:4100/dashboard
```

The dashboard combines live router state with the prompt-free Postgres usage
ledger:

- `/dashboard/api/live` reads health, readiness, router metrics, and routing
  config.
- `/dashboard/api/usage?days=30` reads `gateway_usage_events` for top models,
  daily usage, token counts, estimated spend, top hashed key IDs, and recent
  failures.

The default statistics window is 30 days. The UI also supports 24-hour and
7-day views. It does not display prompts, responses, raw bearer tokens, or raw
session IDs.

## Persistent Usage Ledger

The router emits prompt-free usage events to an internal `usage-ledger` service.
Only the ledger service writes the gateway-owned `gateway_usage_events` table in
the existing Postgres database. The router does not import a Postgres driver or
write SQL directly.

The ledger stores hashed key/session identifiers, model aliases, provider model,
status, latency, token counts when upstream returns them, estimated cost when
pricing is configured, cache status, and fallback metadata. It does not store
prompt bodies, response bodies, raw bearer tokens, or raw session IDs.

Inspect recent rows:

```bash
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
select to_timestamp(timestamp), served_model, status, latency_ms,
       total_tokens, estimated_cost_usd, cache_status, fallback_count
from gateway_usage_events
order by id desc
limit 20;
"
```

## Routing Behavior

Routing is deterministic and config-driven:

1. If the request body contains a `model` value that is in `allowed_models`, the
   router uses that alias.
2. Otherwise, if `X-Session-Id` or the derived fallback session is warm, the
   router reuses the previous successful model for that session.
3. Otherwise, the router uses the configured `default_model`.
4. Retryable upstream failures can move through the configured fallback chain.

Default public aliases:

| Alias level | Examples | Intended use |
| --- | --- | --- |
| Task aliases | `explorer`, `planner`, `coder`, `coder-fast`, `vision` | Default interface for agents and orchestrators. |
| Model-family aliases | `deepseek-v4-flash`, `deepseek-v4-pro`, `glm-5.2`, `kimi-k2.7-code`, `kimi-k2.6` | Caller wants an exact model family and allows provider fallback. |
| Provider deployment aliases | `deepseek-v4-pro-ollama`, `deepseek-v4-pro-deepseek`, `kimi-k2.7-code-opencodego` | Caller wants to force one provider with no gateway fallback. |

The recommended default for package and orchestrator setup is to use task
aliases first. For example, an orchestrator should map planning to `planner`,
building to `coder`, quick edits to `coder-fast`, and search/simple work to
`explorer`. Packages that need direct model selection can request an exact
model-family alias such as `deepseek-v4-pro`, `deepseek-v4-flash`,
`kimi-k2.7-code`, or `kimi-k2.6`. Provider deployment aliases are available as
an escape hatch when a caller needs to force one backend.

The selection rule is: choose a task alias when you know the job, choose a
model-family alias when you know the model and want provider fallback, and
choose a provider deployment alias only when debugging or forcing one backend.

Fallback chains live in `src/gateway.config.yaml` and are generated into the
router and LiteLLM runtime configs. Task aliases target the preferred exact
model-family alias, but their fallback chains use concrete alternate provider
deployments so a provider outage does not immediately route back to the same
provider.

The catalog also records `reasoning_level` as guidance for humans and packages:
`low`, `medium`, or `high`. This field does not rewrite request parameters.

## Configuration

Start from the template:

```bash
cd src
cp .env.example .env
chmod 600 .env
```

Important environment values:

| Variable | Purpose |
| --- | --- |
| `LITELLM_MASTER_KEY` | LiteLLM admin key. Use only for setup and admin API calls. |
| `LITELLM_SALT_KEY` | LiteLLM encryption salt for stored credentials. Do not rotate casually after creating keys or credentials. |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `DATABASE_URL` | Postgres settings for LiteLLM state. |
| `REDIS_PASSWORD`, `REDIS_URL` | Redis auth and connection string for LiteLLM cache and router sessions. |
| `DEEPSEEK_API_KEY` | Provider key for `fast` and `deepseek-pro`. |
| `OPENCODE_GO_API_KEY`, `OPENCODE_GO_API_BASE` | Provider settings for OpenCode Go aliases. |
| `OLLAMA_API_KEY`, `OLLAMA_API_BASE` | Ollama local or cloud settings. |

Edit model and routing values in `src/gateway.config.yaml`, then regenerate the
runtime config files:

```bash
python3 src/scripts/generate_configs.py
python3 src/scripts/generate_opencode_config.py
```

`src/gateway.config.yaml` controls provider model IDs, API base env names,
pricing metadata, LiteLLM cache settings, allowed aliases, fallback chains,
default model, session TTL, retry delays, per-alias timeouts, and aliases
that should receive `prompt_cache_key`. The generated files
`src/litellm.config.yaml` and `src/router/router_config.yaml` are kept committed
for the Docker stack, but they are not the human edit point.

The router validates that configured fallback aliases are allowed and that
allowed aliases exist in the LiteLLM model list.

If you use OpenCode with this gateway, run `generate_opencode_config.py` to sync
the `provider.gateway.models` block in `~/.config/opencode/opencode.json` from
the gateway catalog. It preserves any manually added per-model options while
adding new aliases and updating generated display names. Use `--dry-run` to
preview the merged model list without writing the file.

## Deploy With Docker Compose

From a NAS or other Docker host:

```bash
cd src
cp .env.example .env
# edit .env with real secrets
docker compose up -d --build
docker compose logs -f sticky-router litellm
```

Check service status:

```bash
docker compose ps
curl http://localhost:4100/healthz
curl http://localhost:4100/readyz
```

The agent base URL is:

```text
http://<host>:4100/v1
```

For remote access, prefer Tailscale or another private network. Do not expose
port `4100` directly to the public internet.

## Create And Use Virtual Keys

Agents should use LiteLLM virtual keys, not `LITELLM_MASTER_KEY`. Create a key
from inside the LiteLLM container so port `4000` remains internal:

```bash
cd src

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

Use the returned key as the agent API key.

## Client Usage

OpenAI-compatible clients should point at the router and use a virtual key:

```bash
export OPENAI_BASE_URL=http://<host>:4100/v1
export OPENAI_API_KEY=<litellm-virtual-key>
```

Codex CLI can use the same values through environment variables or a custom
model provider configuration. Codex Pro is a client entitlement, not an
upstream provider behind this gateway.

## Request Examples

List models:

```bash
curl http://localhost:4100/v1/models \
  -H "Authorization: Bearer $VIRTUAL_KEY"
```

Chat completion:

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

Force an allowed alias:

```bash
curl http://localhost:4100/v1/chat/completions \
  -H "Authorization: Bearer $VIRTUAL_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Session-Id: explicit-model-test" \
  -d '{
    "model": "deepseek-pro",
    "messages": [{"role": "user", "content": "explain this design tradeoff"}],
    "max_tokens": 200
  }'
```

Stream SSE chunks:

```bash
curl -N http://localhost:4100/v1/chat/completions \
  -H "Authorization: Bearer $VIRTUAL_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Session-Id: stream-test" \
  -d '{
    "stream": true,
    "messages": [{"role": "user", "content": "write a short haiku"}],
    "max_tokens": 100
  }'
```

Inspect debug metrics:

```bash
curl http://localhost:4100/metrics | python3 -m json.tool
```

## Local Development

Create a development environment from the repo root:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r src/router/requirements-dev.txt
```

Run the router locally against a running LiteLLM instance:

```bash
export LITELLM_BASE_URL=http://localhost:4000
export REDIS_URL=
export DATABASE_URL=
uvicorn router.main:app --app-dir src --host 0.0.0.0 --port 4100 --reload
```

When `REDIS_URL` is unset, the router uses an in-memory session store. That is
useful for tests and local development, but production should use Redis.

Quality checks:

```bash
python3 -m pytest -q
python3 -m ruff check .
python3 -m ruff format --check .
python3 -m mypy
```

Coverage is configured with an 80% minimum:

```bash
python3 -m pytest --cov -q
```

## Operations

The NAS runbook in `src/README.md` includes the longer operational commands for
setup, virtual keys, updates, backup and restore, rollback, secret rotation,
daily spend summaries, and Tailscale-only exposure.

Common commands:

```bash
cd src

# Start or update
docker compose up -d --build

# Tail logs
docker compose logs -f sticky-router litellm

# Restart only the router after router config/code changes
docker compose restart sticky-router

# Restart LiteLLM after provider secret/model config changes
docker compose restart litellm

# Stop without deleting Postgres/Redis volumes
docker compose down
```

Avoid `docker compose down -v` unless you intentionally want to delete virtual
keys, spend history, and Redis cache/session data.

## Security Notes

- Keep `.env` uncommitted and preferably only on the NAS or in a password
  manager.
- Use one LiteLLM virtual key per agent or tool, with model allowlists and
  budgets.
- Do not give agents the master key.
- Do not publish the gateway directly to the internet. Use Tailscale,
  WireGuard, or Cloudflare Access.
- Prompt redaction catches obvious secrets such as `*_API_KEY=...`,
  `*_TOKEN=...`, `*_PASSWORD=...`, `sk-...`, and Ollama-style cloud tokens, but
  it is not a substitute for secret hygiene.
- Router logs store metadata such as session id hashes, selected model, status,
  latency, and fallback count. They do not intentionally log full prompt bodies.

## Troubleshooting

Health is degraded:

```bash
curl http://localhost:4100/healthz | python3 -m json.tool
docker compose ps
docker compose logs --tail=100 sticky-router litellm redis postgres
```

Readiness returns `503`:

- `litellm: degraded` usually means LiteLLM is still starting or its config/env
  is invalid.
- `redis: degraded` usually means `REDIS_URL` or `REDIS_PASSWORD` is wrong.
- `postgres: degraded` usually means `DATABASE_URL`, Postgres credentials, or
  the Postgres service is unavailable.

An agent receives `403`:

- Check whether the LiteLLM virtual key is allowed to use the requested or
  routed alias.
- Try `/v1/models` with the same virtual key.

Requests route to an unexpected model:

- Check `X-Gateway-Reason`.
- If it is `warm-session`, change `X-Session-Id` or wait for
  `cache_ttl_seconds` to expire.
- If it is `explicit-model`, the request body supplied an allowed `model`.
- If it is `default-model`, set `model` explicitly in the client or change
  `default_model` in `src/gateway.config.yaml`, then regenerate configs.

Fallbacks are happening:

- Inspect `X-Gateway-Fallback-*` headers.
- Check `/metrics` for `provider_availability`.
- Tail LiteLLM logs for provider status codes and auth errors.

Router startup fails after config edits:

- Run `python3 src/scripts/generate_configs.py` and check for validation errors.
- Ensure every fallback target in `src/gateway.config.yaml` names a configured
  model alias.

## Current Scope And Future Work

In scope today:

- OpenAI-compatible chat completions and model listing.
- Sticky model sessions.
- Deterministic local routing.
- Router-level retries for retryable failures.
- Streaming passthrough.
- Secret redaction before provider egress.
- Health, readiness, metrics, logs, virtual-key usage, and NAS runbook.

Not implemented yet:

- `/v1/responses`, embeddings, images, audio, or assistants-style APIs.
- A working `vision` alias.
- Public internet hardening as a standalone exposed service.
- Copilot Pro as an upstream provider.
- Persisted historical metrics outside LiteLLM/Postgres.

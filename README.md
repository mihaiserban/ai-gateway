# agent-ai-gateway

Personal OpenAI-compatible AI gateway for local agents. The stack exposes one
stable endpoint, serves a live model catalog (combos, registry models, and
connection models), keeps warm conversations on the same deployment, redacts
obvious secrets before provider egress, and delegates provider adapters,
virtual keys, caching, budgets, and spend tracking to LiteLLM.

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
  +--> Ollama (local/cloud)
  +--> DeepSeek
  +--> OpenCode Go
  +--> future providers

Redis     -> LiteLLM cache state and router session stickiness
Postgres  -> LiteLLM virtual keys, budgets, spend, and UI state
```

Only the router publishes a host port by default. The compose file maps
`4100:4100` on the Docker host; keep that host behind Tailscale, a VPN, or a
private LAN firewall. LiteLLM, Postgres, and Redis stay internal to Docker.
For localhost-only development, change the router port mapping to
`127.0.0.1:4100:4100`.

## Repository Layout

```text
.
+-- README.md                  # project overview and usage
+-- docs/                      # planning, status notes, runbooks, reports
+-- pyproject.toml             # lint, type-check, test, and coverage config
+-- src/
    +-- docker-compose.yml     # runtime stack
    +-- gateway.config.yaml    # human-edited providers, connections, combos, router, LiteLLM
    +-- litellm.config.yaml    # generated LiteLLM model deployments and cache settings
    +-- .env.example           # secret/config template
    +-- README.md              # NAS-oriented operations runbook
    +-- scripts/
        +-- gateway.py         # gateway CLI: generate, doctor, explain, models, setup
        +-- generate_configs.py # legacy regenerator (delegates to gateway.py generate)
    +-- router/
        +-- main.py            # FastAPI sticky router
        +-- routing.py         # routing and fallback decisions
        +-- redaction.py       # outbound prompt secret redaction
        +-- sessions.py        # Redis or memory session store
        +-- health.py          # dependency health checks
        +-- metrics.py         # in-memory debug metrics
        +-- router_config.yaml # generated router deployments, TTLs, timeouts
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
| `/v1/chat/completions` | `POST` | Resolves the requested model to an ordered deployment list, redacts prompt secrets, forwards to LiteLLM, supports non-streaming and SSE streaming responses, and falls back to the next deployment on retryable upstream failures. |
| `/v1/models` | `GET` | Returns the live model catalog (combos, registry models, connection models) filtered by active deployments. Use `?view=all\|combos\|registry\|connections` to select a slice. |
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
| `X-Gateway-Requested-Model` | The model id the caller requested. |
| `X-Gateway-Model-Kind` | `combo`, `registry-model`, or `connection-model`. |
| `X-Gateway-Served-Deployment` | The deployment id that served the response (e.g. `ollama-cloud.kimi-k2.7-code`). |
| `X-Gateway-Fallback-Count` | Number of fallback hops (0 when no fallback). |
| `X-Gateway-Attempted-Models` | Comma-separated deployment ids tried, in order. |
| `X-Gateway-Fallback-From` | First attempted deployment id, present only after a fallback. |

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

## Model Kinds

The gateway organizes models in five kinds. Each kind answers a different
question about where a model id comes from and how it is routed.

| Kind | Description |
| --- | --- |
| **Provider** | Reusable adapter and registry metadata. Declares the LiteLLM prefix, API base/key env vars, and the catalog of model ids that provider can serve. |
| **Connection** | One configured local endpoint, account, or key for a provider. Has priority, stability, and concurrency knobs and selects which registry models it serves. |
| **Combo** | Curated public model with fallback/scoring. A stable id (`explorer`, `planner`, `coder`, ...) that maps to ordered `(connection, model)` candidates and a scoring policy. |
| **Registry model** | A provider model id served by active connections. Examples: `kimi-k2.7-code`, `deepseek-v4-pro`, `glm-5.2`. The router resolves it to the best active deployment at request time. |
| **Connection model** | Explicit qualified deployment id in the form `<connection>.<model>`, e.g. `ollama-cloud.kimi-k2.7-code`. Forces one connection with no combo/registry fallback. |
| **Client** | Local harness setup target (`codex`, `claude-code`, `opencode`, `pi`). Used by the `gateway setup` CLI to render harness config from the live catalog. |

Combos and registry models share the `/v1/models` namespace. See
[docs/models.md](docs/models.md) for the full provider/connection/combo
reference, live catalog tables, and scoring weights.

## Routing Behavior

Routing is deterministic and config-driven:

1. The router resolves the requested model id to an ordered list of
   deployments:
   - **Combo** -> its candidate `(connection, model)` pairs, scored per the
     combo's scoring weights (health, latency, quota, stability, connection
     density, priority) and filtered to active connections.
   - **Registry model** -> every active connection whose provider registry
     lists that model id, ordered by the same scoring inputs.
   - **Connection model** -> exactly the one named deployment.
2. If no `model` is supplied and `X-Session-Id` is warm, the router reuses the
   previous successful deployment for that session.
3. If no `model` is supplied and the session is cold, the router uses the
   configured `default_model` (`coder` by default).
4. Deployments are tried in order. Fallback only happens on retryable upstream
   failures (capacity, quota, billing, entitlement, missing-model,
   context-limit, unsupported-parameter). Caller-side auth, virtual-key
   budget, virtual-key model allowlist, and malformed-request errors do not
   fall back.

LiteLLM is an execution adapter: the router rewrites catalog ids to the
internal deployment id (e.g. `ollama-cloud.kimi-k2.7-code`) before calling
LiteLLM. Because of this, LiteLLM virtual keys must allow the internal
deployment ids they may use, such as `ollama-cloud.kimi-k2.7-code` and
`deepseek-api.deepseek-v4-pro`. When a combo or registry model falls back,
the served deployment may be any of its candidates, so include every allowed
deployment id in the virtual key allowlist. Use
`python3 src/scripts/gateway.py explain <model>` to list candidate deployments.

The catalog also records `reasoning_level` as guidance for humans and
packages: `low`, `medium`, or `high`. This field does not rewrite request
parameters.

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
| `DEEPSEEK_API_KEY` | Provider key for `deepseek-api.*` deployments. |
| `OPENCODE_GO_API_KEY`, `OPENCODE_GO_API_BASE` | Provider settings for `opencode-go.*` deployments. |
| `OLLAMA_API_KEY`, `OLLAMA_API_BASE` | Ollama Cloud settings for `ollama-cloud.*` deployments. |
| `VIRTUAL_KEY` | Default LiteLLM virtual key written into client configs by `gateway setup`. |

Edit providers, connections, combos, router, and LiteLLM values in
`src/gateway.config.yaml`, then regenerate the runtime config files:

```bash
python3 src/scripts/gateway.py generate
```

`src/gateway.config.yaml` controls providers (adapter + registry metadata),
connections (enabled endpoints, priority, stability, concurrency, model
selection), combos (curated public models with candidate deployments and
scoring weights), router knobs (default model, cache TTL, retry, quota
cooldown), and LiteLLM settings (cache, logging, retries). The generated files
`src/litellm.config.yaml` and `src/router/router_config.yaml` are kept
committed for the Docker stack, but they are not the human edit point.

The router validates the catalog at startup and the `gateway doctor` command
reports missing env vars, inactive connections, and combo/registry
consistency.

### Gateway CLI cookbook

```bash
# Validate providers, connections, combos, and env vars
set -a; . src/.env; set +a   # or export the same values in your shell
python3 src/scripts/gateway.py doctor

# Regenerate runtime configs from gateway.config.yaml
python3 src/scripts/gateway.py generate

# Print the live catalog
python3 src/scripts/gateway.py models --view all
python3 src/scripts/gateway.py models --view combos

# Explain how a model id resolves to deployments
python3 src/scripts/gateway.py explain kimi-k2.7-code

# Preview and apply a client config
python3 src/scripts/gateway.py setup codex --catalog all --dry-run
python3 src/scripts/gateway.py setup opencode --mode local-plugin --catalog all --apply

# Query the live catalog over HTTP
curl "http://localhost:4100/v1/models?view=all" -H "Authorization: Bearer $VIRTUAL_KEY"
```

`gateway.py doctor` checks the current shell environment. If you have only
created `src/.env`, export it first as shown above. The command verifies that
required variables are present; the chat smoke tests below still require real,
non-empty provider keys or a reachable Ollama endpoint.

If you use OpenCode with this gateway, configure it with the gateway CLI. The
default `local-plugin` mode installs a first-party plugin that fetches the live
catalog from `/v1/models?view=<catalog>` at startup; `static` mode writes a
snapshot of the current catalog into `provider.gateway.models`:

```bash
python3 src/scripts/gateway.py setup opencode --mode local-plugin --catalog all --dry-run
python3 src/scripts/gateway.py setup opencode --mode local-plugin --catalog all --apply
python3 src/scripts/gateway.py setup opencode --mode static --catalog all --apply
```

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
    'models': ['ollama-cloud.kimi-k2.7-code', 'deepseek-api.deepseek-v4-pro', 'opencode-go.kimi-k2.7-code'],
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

Because the router rewrites catalog ids to deployment ids before calling
LiteLLM, LiteLLM virtual keys must allow the internal deployment ids they may
use, such as `ollama-cloud.kimi-k2.7-code` and
`deepseek-api.deepseek-v4-pro`. When a combo or registry model falls back, the
served deployment may be any of its candidates, so the virtual key allowlist
must include every deployment id the agent is allowed to use. Use
`python3 src/scripts/gateway.py explain <model>` to list the candidate
deployments for a combo or registry model.

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

These examples require a LiteLLM virtual key in `VIRTUAL_KEY`. Create one with
the command in [Create And Use Virtual Keys](#create-and-use-virtual-keys), or
export an existing key before running the curls. Chat requests also require at
least one candidate provider for the requested model to be reachable and
authenticated.

List models (live catalog, all views):

```bash
curl "http://localhost:4100/v1/models?view=all" \
  -H "Authorization: Bearer $VIRTUAL_KEY"
```

Chat completion (uses the default combo `coder`):

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

Force a registry model (provider fallback allowed):

```bash
curl http://localhost:4100/v1/chat/completions \
  -H "Authorization: Bearer $VIRTUAL_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Session-Id: explicit-model-test" \
  -d '{
    "model": "kimi-k2.7-code",
    "messages": [{"role": "user", "content": "explain this design tradeoff"}],
    "max_tokens": 200
  }'
```

Force one connection (no fallback):

```bash
curl http://localhost:4100/v1/chat/completions \
  -H "Authorization: Bearer $VIRTUAL_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Session-Id: connection-model-test" \
  -d '{
    "model": "ollama-cloud.kimi-k2.7-code",
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

A `Makefile` in the repo root wraps the most common NAS operations:

```bash
cd /volume1/docker/ai-gateway

make regen               # regenerate runtime configs from gateway.config.yaml
make regen-redeploy      # regenerate configs and redeploy the full stack
make redeploy            # rebuild and restart the full stack
make update              # pull fresh images and redeploy
make restart-router      # restart only the router
make restart-litellm     # restart only LiteLLM
make health              # show service status + healthz/readyz
make logs                # tail sticky-router and litellm logs
VIRTUAL_KEY=... make smoke
```

Manual equivalents for reference:

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
- During deploys, the router container may start before LiteLLM is ready;
  `/readyz` remains `503` until LiteLLM finishes warming up.
- `redis: degraded` usually means `REDIS_URL` or `REDIS_PASSWORD` is wrong.
- `postgres: degraded` usually means `DATABASE_URL`, Postgres credentials, or
  the Postgres service is unavailable.

An agent receives `403`:

- Check whether the LiteLLM virtual key allowlist includes the deployment id
  the router tried. The router rewrites catalog ids to deployment ids before
  calling LiteLLM, so the allowlist must include deployment ids such as
  `ollama-cloud.kimi-k2.7-code`, not just the combo or registry model id.
- Run `python3 src/scripts/gateway.py explain <model>` to list the candidate
  deployments, then update the virtual key allowlist.
- Try `/v1/models?view=all` with the same virtual key.

Requests route to an unexpected deployment:

- Inspect `X-Gateway-Requested-Model`, `X-Gateway-Model-Kind`, and
  `X-Gateway-Served-Deployment`.
- If `X-Gateway-Served-Deployment` differs from the requested model, a
  fallback occurred; inspect `X-Gateway-Attempted-Models` for the full try
  order.
- If no `model` was supplied and the session was warm, the router reused the
  previous successful deployment. Change `X-Session-Id` or wait for
  `cache_ttl_seconds` to expire to re-resolve.
- If no `model` was supplied and the session was cold, the router used
  `default_model` (`coder` by default). Set `model` explicitly or change
  `default_model` in `src/gateway.config.yaml`, then regenerate configs.

Fallbacks are happening:

- Inspect `X-Gateway-Fallback-Count` and `X-Gateway-Attempted-Models`.
- Check `/metrics` for `provider_availability`.
- Tail LiteLLM logs for provider status codes and auth errors.

Router startup fails after config edits:

- Run `python3 src/scripts/gateway.py doctor` and check for validation
  errors.
- Run `python3 src/scripts/gateway.py generate` to regenerate runtime configs.
- Ensure every combo candidate in `src/gateway.config.yaml` names a connection
  and registry model that exist.

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

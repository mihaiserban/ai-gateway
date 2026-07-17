# Synology AI Gateway

This runs a small personal AI gateway stack with Docker:

- Sticky FastAPI router on port `4100`
- LiteLLM proxy on internal port `4000`
- Postgres for LiteLLM virtual keys, spend tracking, and UI state
- Redis for LiteLLM cache state and router session stickiness (see [docs/redis.md](../docs/redis.md))

Agents talk to the router. The router resolves the requested model (combo,
registry model, or connection model) to an ordered deployment list, keeps warm
sessions on the same deployment, redacts obvious secrets, and forwards to
LiteLLM. LiteLLM is an execution adapter: the router rewrites catalog ids to
internal deployment ids (e.g. `ollama-cloud.kimi-k2.7-code`) before calling
LiteLLM. LiteLLM handles provider adapters, virtual keys, caching, and spend
tracking.

## Files

```text
src/
  docker-compose.yml
  gateway.config.yaml     # human-edited providers, connections, combos, router, LiteLLM
  litellm.config.yaml     # generated LiteLLM deployments and cache settings
  .env.example
  README.md
  scripts/
    gateway.py            # gateway CLI: generate, doctor, explain, models, setup
    generate_configs.py   # legacy regenerator (delegates to gateway.py generate)
  router/
    Dockerfile
    requirements.txt
    main.py
    redaction.py
    routing.py
    router_config.yaml    # generated router deployments, TTLs, timeouts
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
- `OLLAMA_API_BASE=https://ollama.com` for the default `ollama-cloud` connection
- Optional: `GATEWAY_MAX_REQUEST_BODY_BYTES` to override the router request
  body limit before JSON parsing; default is `10485760` (10 MiB).

The checked-in catalog names the Ollama connection `ollama-cloud`. If you want
to run a separate local Ollama connection later, add it as a distinct
connection instead of reusing the cloud deployment ids.

Start it:

```bash
docker volume inspect ai-gateway_postgres_data >/dev/null 2>&1 || \
  docker volume create ai-gateway_postgres_data
docker compose up -d --build
docker compose logs -f sticky-router litellm
```

The external `ai-gateway_postgres_data` volume stores LiteLLM state and the
usage ledger. It survives removal and recreation of the Compose project. An
existing deployment already using the default `ai-gateway_postgres_data`
volume is adopted in place, so its rows do not need to be copied.

## Agent Endpoint

The OpenAI-compatible endpoint for agents is:

```text
http://<nas-host>:4100/v1
```

Health check:

```bash
curl http://localhost:4100/healthz
```

Before running model discovery or chat as an agent, create a LiteLLM virtual
key in [Virtual Keys And Model Allowlists](#virtual-keys-and-model-allowlists)
or export an existing one:

```bash
export VIRTUAL_KEY=<litellm-virtual-key>
```

Model discovery uses that virtual key. The router returns the live catalog
filtered to active deployments. Use `?view=` to select a slice:

```bash
curl "http://localhost:4100/v1/models?view=all" \
  -H "Authorization: Bearer $VIRTUAL_KEY"
curl "http://localhost:4100/v1/models?view=combos" \
  -H "Authorization: Bearer $VIRTUAL_KEY"
curl "http://localhost:4100/v1/models?view=registry" \
  -H "Authorization: Bearer $VIRTUAL_KEY"
curl "http://localhost:4100/v1/models?view=connections" \
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

The chat smoke tests require at least one candidate provider for the requested
model to be reachable and authenticated. If every provider key is blank or the
Ollama Cloud endpoint/key is wrong, health and model discovery can still pass
while chat returns `502 gateway_upstream_exhausted`.

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

- `X-Gateway-Requested-Model`: the model id the caller requested
- `X-Gateway-Model-Kind`: `combo`, `registry-model`, or `connection-model`
- `X-Gateway-Served-Deployment`: the deployment id that served the response (e.g. `ollama-cloud.kimi-k2.7-code`)
- `X-Gateway-Fallback-Count`: number of fallback hops (0 when no fallback)
- `X-Gateway-Attempted-Models`: comma-separated deployment ids tried, in order
- `X-Gateway-Fallback-From`: first attempted deployment id, only present when a fallback occurred

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
container restarts. It reports request totals, requested and served
deployments, fallback count, LiteLLM cache counts (`hit`, `miss`, `unknown`),
and per-deployment upstream availability. Availability is keyed by deployment
id and counts every upstream attempt, including failed attempts before a
fallback:

```json
{
  "provider_availability": {
    "ollama-cloud.kimi-k2.7-code": {
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

## Dashboard

Open the live operations dashboard from the LAN or Tailscale network:

```text
http://<nas-host>:4100/dashboard
```

Use it to check dependency readiness, request volume, fallback behavior, cache
counts, provider availability, token usage by model, estimated spend, and recent
failed upstream attempts. Historical cards use the router-owned
`gateway_usage_events` table and default to the last 30 days.

If the dashboard shows "Usage error" or remains at `Loading...`, check that
Postgres is healthy and that `DATABASE_URL` is correct:

```bash
docker compose ps postgres
curl http://localhost:4100/healthz | python3 -m json.tool
```

The usage summary uses a 5-second connect timeout and returns an empty fallback
view when the ledger database is unreachable, so the rest of the dashboard still
loads.

## Persistent Usage Ledger

The router emits prompt-free usage events to an internal `usage-ledger` service.
Only the ledger service writes the gateway-owned `gateway_usage_events` table in
the existing Postgres database. The router does not import a Postgres driver or
write SQL directly.

The ledger stores hashed key/session identifiers, model aliases, provider model,
status, latency, token counts when upstream returns them, estimated cost when
pricing is configured, cache status, and fallback metadata. It does not store
prompt bodies, response bodies, raw bearer tokens, or raw session IDs.

The `gateway_usage_events` table is stored in the external
`ai-gateway_postgres_data` Docker volume, not in the `usage-ledger` container.
Rebuilding or recreating either application container therefore keeps the
statistics. Do not remove that volume unless you intend to erase the ledger,
LiteLLM virtual keys, budgets, and spend history.

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

## Virtual Keys And Model Allowlists

Agents should use LiteLLM virtual keys, not the master key. Create one key
per agent/tool with a model allowlist so a background summarizer cannot use
an expensive deployment.

Because the router rewrites catalog ids to deployment ids before calling
LiteLLM, LiteLLM virtual keys must allow the internal deployment ids they may
use, such as `ollama-cloud.kimi-k2.7-code` and
`deepseek-api.deepseek-v4-pro`. When a combo or registry model falls back,
the served deployment may be any of its candidates, so the virtual key
allowlist must include every deployment id the agent is allowed to use. Use
`python3 src/scripts/gateway.py explain <model>` to list the candidate
deployments for a combo or registry model.

### Per-Agent Keys

The following virtual keys are provisioned on a fresh stack (re-create them
after a `docker compose down -v` that wipes Postgres):

| Agent / Tool | Key alias | Allowlist (deployment ids) | Max budget |
| --- | --- | --- | --- |
| OpenCode | `all-models` | *(all)* | — |
| Codex CLI | `codex-cli` | `ollama-cloud.kimi-k2.7-code`, `deepseek-api.deepseek-v4-pro`, `opencode-go.kimi-k2.7-code` | $5.00 |
| Summarizer | `summarizer` | `ollama-cloud.deepseek-v4-flash`, `deepseek-api.deepseek-v4-flash` | $2.00 |

The `summarizer` key is intentionally restricted to the cheaper flash
deployments so a background summarizer cannot spend on `deepseek-v4-pro` or
the coding combos.

### Creating A Virtual Key

Create a virtual key (admin only, with the master key):

```bash
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

Use the returned `key` value as the `Authorization: Bearer <key>` for that
agent. The `models` list must contain deployment ids, not catalog ids. Run
`python3 src/scripts/gateway.py explain <model>` from the repo to list the
candidate deployments for a combo or registry model.

To create a key that works with **all** gateway models (no model allowlist,
no budget cap), pass an empty `models` array and `max_budget: None`:

```bash
docker compose exec litellm python3 -c "
import os, json, urllib.request
body = json.dumps({
    'key_alias': 'all-models',
    'models': [],
    'max_budget': None
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

An empty `models` array means "no restriction" — the key can call any
model or deployment the gateway exposes. Use this for the OpenCode plugin
or other clients that need to see and use the full live catalog.

The master key is only for admin setup (creating keys, viewing spend). Do not
put it in agent configs.

## Codex CLI Config

Point Codex at the gateway with a custom model provider or
`OPENAI_BASE_URL`:

```bash
export OPENAI_BASE_URL=http://<nas-host>:4100/v1
export OPENAI_API_KEY=<litellm-virtual-key>
```

Or render the Codex config from the live catalog with the gateway CLI:

```bash
python3 src/scripts/gateway.py setup codex --catalog all --dry-run
python3 src/scripts/gateway.py setup codex --catalog all --apply
```

Codex Pro is a client entitlement; it calls this gateway as a client and is
not configured as an upstream provider.

## Live Model Catalog

The gateway exposes three kinds of model ids in `/v1/models`. See
[docs/models.md](../docs/models.md) for the full reference, live provider
catalog tables, and scoring weights.

| Kind | Examples | Use when |
| --- | --- | --- |
| Combo | `explorer`, `planner`, `coder`, `coder-fast`, `vision` | A tool or orchestrator wants the gateway's recommended default for a job. |
| Registry model | `deepseek-v4-flash`, `deepseek-v4-pro`, `glm-5.2`, `kimi-k2.6`, `kimi-k2.7-code` | A caller wants an exact model family with provider fallback. |
| Connection model | `ollama-cloud.kimi-k2.7-code`, `deepseek-api.deepseek-v4-pro` | A caller needs to force or debug one connection with no fallback. |

Use combos first. Use a registry model when you know the model family. Use a
connection model only when debugging or forcing one backend.

Catalog entries include `gateway.context_length` when active deployments report
it. Combos expose the minimum active candidate context, and static OpenCode
setup maps it to `limit.context` for harness context-window displays.

### Combos

All combos live under `combos:` in `src/gateway.config.yaml` and use scored
candidate deployments. See [docs/models.md](../docs/models.md#combos) for the
full candidate list and scoring weights.

| Combo | Task | Primary deployment | Purpose |
| --- | --- | --- | --- |
| `explorer` | explore | `ollama-cloud.deepseek-v4-flash` | Fast/cheap search, simple tasks |
| `planner` | plan | `ollama-cloud.glm-5.2` | Strong reasoning, planning, analysis |
| `coder` | build | `ollama-cloud.kimi-k2.7-code` | Primary coding workhorse (default model) |
| `coder-fast` | quick-build | `ollama-cloud.deepseek-v4-flash` | Quick edits, commits |
| `vision` | vision | `ollama-cloud.kimi-k2.6` | Multimodal image understanding |

Copilot Pro is intentionally skipped for now. Codex Pro is a client that can
call this gateway; it is not configured as an upstream provider.

## Routing

The router resolves the requested model id to an ordered deployment list:

- **Combo** -> its candidate `(connection, model)` pairs, scored per the
  combo's scoring weights and filtered to active connections.
- **Registry model** -> every active connection whose provider registry lists
  that model id, ordered by the same scoring inputs.
- **Connection model** -> exactly the one named deployment.

If no `model` is supplied, a warm `X-Session-Id` reuses the previous
successful deployment; otherwise the configured `default_model` (`coder`) is
used.

Fallbacks trigger for provider-side capacity, quota, billing, entitlement,
missing-model, context-limit, and unsupported-parameter errors. Caller-side
auth, virtual-key budget, virtual-key model allowlist, and malformed request
errors do not fallback.

Default session TTL is 600 seconds. See [docs/redis.md](../docs/redis.md) for
full Redis architecture and cache behavior. Transient Redis session failures
are logged and degrade to cold routing instead of failing chat requests.

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

From the repo root, use the provided Makefile:

```bash
cd /volume1/docker/ai-gateway

# Regenerate runtime configs after editing gateway.config.yaml
make regen

# Redeploy the full stack after code/config/router changes
make redeploy

# Regenerate + redeploy in one step after gateway.config.yaml changes
make regen-redeploy

# Pull fresh images and redeploy
make update

# Restart only the router or LiteLLM when only one service changed
make restart-router
make restart-litellm

# Tail logs, check health, or run a smoke test
make logs
make health
VIRTUAL_KEY=... make smoke
```

For the old manual commands, see the previous version of this runbook.

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
- Agents use `http://<tailscale-ip>:4100/v1`.
- Only port `4100` is published by the router service; `4000`, `5432`, and
  `6379` stay on the internal Docker network.
- If you need remote access without Tailscale, use a Cloudflare Tunnel with
  Access policies in front of it — never a raw port forward.

## Local Tests

```bash
python3 -m pip install -r router/requirements-dev.txt
PYTHONPATH=. python3 -m pytest router/tests ledger/tests -q
cd .. && node --test src/clients/opencode_plugin/index.test.mjs
```

Back up the Docker volumes before major upgrades:

- `ai-gateway_postgres_data`
- `ai-gateway_redis_data`

## Notes

- `LITELLM_SALT_KEY` is used to encrypt stored provider credentials. Do not
  rotate it casually after creating models or keys.
- Keep `.env` only on the NAS or in a password manager.
- Put provider, connection, and combo changes in `gateway.config.yaml`, then
  run `make regen` from the repo root (or
  `python3 src/scripts/gateway.py generate`), and redeploy.
- LiteLLM virtual key allowlists must contain deployment ids
  (`ollama-cloud.kimi-k2.7-code`), not catalog ids, because the router
  rewrites catalog ids to deployment ids before calling LiteLLM.
- LiteLLM can warn that custom model costs are missing. That does not block
  calls; add `model_info` pricing later if exact spend reporting matters.

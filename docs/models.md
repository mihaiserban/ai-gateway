# Model reference

This page documents the live model catalog and provider wiring in the personal
AI gateway, plus the live catalogs fetched from each provider.

The canonical machine-readable source of truth is
`src/gateway.config.yaml`. After editing it, regenerate runtime configs:

```bash
python3 src/scripts/gateway.py generate
```

## Model kinds

The gateway organizes models in five kinds. Each kind answers a different
question about where a model id comes from and how it is routed.

| Kind | Description |
| --- | --- |
| **Provider** | Reusable adapter and registry metadata. Declares the LiteLLM prefix, API base/key env vars, and the catalog of model ids that provider can serve. |
| **Connection** | One configured local endpoint, account, or key for a provider. Has priority, stability, and concurrency knobs and selects which registry models it serves. |
| **Combo** | Curated public model with fallback/scoring. A stable id (`explorer`, `planner`, `coder`, ...) that maps to ordered `(connection, model)` candidates and a scoring policy. Shares the `/v1/models` namespace with registry model ids. |
| **Registry model** | A provider model id served by active connections. Examples: `kimi-k2.7-code`, `deepseek-v4-pro`, `glm-5.2`. The router resolves it to the best active deployment at request time. |
| **Connection model** | Explicit qualified deployment id in the form `<connection>.<model>`, e.g. `ollama-cloud.kimi-k2.7-code`. Forces one connection with no combo/registry fallback. |
| **Client** | Local harness setup target (`codex`, `claude-code`, `opencode`, `pi`). Used by the `gateway setup` CLI to render harness config from the live catalog. |

### How a requested model is resolved

1. The router resolves the requested model id to an ordered list of
   deployments:
   - **Combo** -> its candidate `(connection, model)` pairs, scored per the
     combo's `scoring` weights (health, latency, quota, stability, connection
     density, priority) and filtered to active connections.
   - **Registry model** -> every active connection whose provider registry
     lists that model id, ordered by the same scoring inputs.
   - **Connection model** -> exactly the one named deployment.
2. Deployments are tried in order. Fallback only happens on retryable upstream
   failures (capacity, quota, billing, entitlement, missing-model,
   context-limit, unsupported-parameter). Caller-side auth, virtual-key
   budget, virtual-key model allowlist, and malformed-request errors do not
   fall back.
3. LiteLLM is an execution adapter: the router rewrites catalog ids to the
   internal deployment id (e.g. `ollama-cloud.kimi-k2.7-code`) before calling
   LiteLLM.

`model_info.reasoning_level` is catalog metadata with values `none`, `low`,
`medium`, or `high`. It is guidance for humans and packages; the router does
not translate it into provider-specific request parameters.

`/v1/models` exposes `gateway.context_length` for combos, registry models, and
connection models when the active deployments report it. Combos use the minimum
context length across their active candidates so harnesses can display context
usage conservatively.

### Recommended orchestrator mapping

| Orchestrator role | Combo | Reasoning level |
| --- | --- | --- |
| Explore/search/simple work | `explorer` | `low` |
| Plan/reason/analyze | `planner` | `high` |
| Build/code | `coder` | `medium` |
| Quick edits/commits | `coder-fast` | `low` |
| Image input | `vision` | `medium` |

The recommended default is to use combos first. Use a registry model when you
want an exact model family with provider fallback. Use a connection model only
when debugging or forcing one backend.

## Response headers

Successful chat responses include gateway routing headers:

| Header | Meaning |
| --- | --- |
| `X-Gateway-Requested-Model` | The model id the caller requested. |
| `X-Gateway-Model-Kind` | `combo`, `registry-model`, or `connection-model`. |
| `X-Gateway-Served-Deployment` | The deployment id that actually served the response (e.g. `ollama-cloud.kimi-k2.7-code`). |
| `X-Gateway-Fallback-Count` | Number of fallback hops (0 when no fallback). |
| `X-Gateway-Attempted-Models` | Comma-separated deployment ids tried, in order. Empty when no fallback. |
| `X-Gateway-Fallback-From` | First attempted deployment id, present only after a fallback. |

LiteLLM cache headers such as `x-litellm-cache-hit` and `x-litellm-cache-key`
are preserved when the upstream returns them.

## Provider wiring

Providers live under `providers:` in `src/gateway.config.yaml`. Each provider
declares its LiteLLM adapter prefix, API base/key env vars, optional drop
params, and a `registry.models` map of the model ids it can serve.

| Provider | LiteLLM prefix | API base env | API key env | Notes |
| --- | --- | --- | --- | --- |
| `ollama` | `ollama_chat/*` | `OLLAMA_API_BASE` | `OLLAMA_API_KEY` | Ollama Cloud; primary path for most models. |
| `deepseek` | `deepseek/*` | n/a | `DEEPSEEK_API_KEY` | Paid API fallback. |
| `opencode-go` | `openai/*` | `OPENCODE_GO_API_BASE` | `OPENCODE_GO_API_KEY` | OpenAI-compatible adapter; drops `reasoningSummary`. |

To add a provider, add a new key under `providers:` with its adapter prefix,
env vars, and `registry.models`, then add one or more `connections:` that
enable it and run `python3 src/scripts/gateway.py generate`.

## Connections

Connections live under `connections:` in `src/gateway.config.yaml`. Each
connection binds a provider to one endpoint/account/key and selects which
registry models it serves.

| Connection | Provider | Priority | Stability | Max concurrent | Models |
| --- | --- | --- | --- | --- | --- |
| `ollama-cloud` | `ollama` | 10 | 0.85 | 8 | all |
| `deepseek-api` | `deepseek` | 30 | 0.90 | 20 | `deepseek-v4-flash`, `deepseek-v4-pro` |
| `opencode-go` | `opencode-go` | 40 | 0.70 | 8 | all |

To add a connection, add a key under `connections:` with `provider:`,
`enabled:`, `priority:`, `stability:`, `max_concurrent:`, and `models:`
(either `all` or a list of registry model ids), then regenerate.

## Combos

Combos live under `combos:` in `src/gateway.config.yaml`. Each combo is a
curated public model id with ordered `(connection, model)` candidates and a
scoring policy. Combos share the `/v1/models` namespace with registry model
ids.

| Combo | Task | Candidates (connection.model) |
| --- | --- | --- |
| `explorer` | explore | `ollama-cloud.deepseek-v4-flash`, `deepseek-api.deepseek-v4-flash`, `opencode-go.deepseek-v4-flash` |
| `planner` | plan | `ollama-cloud.glm-5.2`, `opencode-go.glm-5.2`, `deepseek-api.deepseek-v4-pro`, `opencode-go.kimi-k2.7-code` |
| `coder` | build | `ollama-cloud.kimi-k2.7-code`, `deepseek-api.deepseek-v4-pro`, `opencode-go.kimi-k2.7-code` |
| `coder-fast` | quick-build | `ollama-cloud.deepseek-v4-flash`, `deepseek-api.deepseek-v4-flash`, `opencode-go.deepseek-v4-flash`, `opencode-go.kimi-k2.6` |
| `vision` | vision | `ollama-cloud.kimi-k2.6`, `opencode-go.kimi-k2.6` |

All combos use the same scoring weights:

| Input | Weight |
| --- | --- |
| `health` | 0.30 |
| `latency` | 0.20 |
| `quota` | 0.15 |
| `stability` | 0.15 |
| `connection_density` | 0.10 |
| `priority` | 0.10 |

To add a combo, add a key under `combos:` with `task:`, `strategy: score`,
`candidates:` (ordered list of `{connection, model}`), and `scoring:`, then
regenerate.

## Live catalog views

`/v1/models` returns the live catalog filtered to active deployments. Pass
`?view=` to select a slice:

| View | Contents |
| --- | --- |
| `all` (default) | combos + registry models + connection models |
| `combos` | combos only |
| `registry` | registry models only |
| `connections` | connection models only |

## Clients

Clients live under `clients:` in `src/gateway.config.yaml`. Each client is a
local harness setup target with a default model, catalog view, and the config
file paths the `gateway setup` CLI writes.

| Client | Default model | Catalog | Config path |
| --- | --- | --- | --- |
| `codex` | `coder` | `all` | `~/.codex/config.toml`, `~/.codex/codex.toml` |
| `claude-code` | `coder` | `all` | `~/.claude/settings.json` |
| `opencode` | `coder` (small: `coder-fast`) | `all` | `~/.config/opencode/opencode.json` |
| `pi` | `coder` | `all` | `~/.config/pi/settings.json` |

## Cookbook

```bash
# Validate providers, connections, combos, and env vars
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

## Virtual key allowlists

Because the router rewrites catalog ids to deployment ids before calling
LiteLLM, LiteLLM virtual keys must allow the internal deployment ids they may
use, such as `ollama-cloud.kimi-k2.7-code` and
`deepseek-api.deepseek-v4-pro`.

When a combo or registry model falls back, the served deployment may be any of
its candidates, so the virtual key allowlist must include every deployment id
the agent is allowed to use. Use `python3 src/scripts/gateway.py explain
<model>` to list the candidate deployments for a combo or registry model, and
include each allowed deployment id in the `models` list when creating the
virtual key.

## Live provider catalogs

Fetched from each provider's API on **2026-06-30**. Use these to decide which
models to add or retire.

### Ollama (ollama.com/api/tags)

Endpoint: `https://ollama.com/api/tags`

| Model | Size (bytes) | Updated |
| --- | --- | --- |
| `deepseek-v3.1:671b` | 688,586,727,753 | 2025-11-20 |
| `deepseek-v3.2` | 688,586,727,753 | 2025-12-02 |
| `deepseek-v4-flash` | 140,000,000,000 | 2026-04-24 |
| `deepseek-v4-pro` | 1,600,000,000,000 | 2026-04-24 |
| `devstral-2:123b` | 128,249,391,520 | 2025-12-08 |
| `devstral-small-2:24b` | 51,600,000,000 | 2025-12-09 |
| `gemini-3-flash-preview` | 0 | 2025-12-17 |
| `gemma3:12b` | 24,000,000,000 | 2025-03-12 |
| `gemma3:27b` | 55,000,000,000 | 2025-03-12 |
| `gemma3:4b` | 8,600,000,000 | 2025-03-12 |
| `gemma4:31b` | 62,546,177,752 | 2026-04-02 |
| `glm-4.7` | 696,060,000,000 | 2025-12-22 |
| `glm-5` | 756,162,687,872 | 2026-02-11 |
| `glm-5.1` | 1,507,728,316,928 | 2026-04-07 |
| `glm-5.2` | 0 | 2026-06-16 |
| `gpt-oss:120b` | 65,290,180,781 | 2025-08-05 |
| `gpt-oss:20b` | 13,780,162,412 | 2025-08-05 |
| `kimi-k2.5` | 1,118,481,408,000 | 2026-01-26 |
| `kimi-k2.6` | 595,148,192,736 | 2026-03-31 |
| `kimi-k2.7-code` | 595,148,192,736 | 2026-06-12 |
| `minimax-m2.1` | 230,000,000,000 | 2025-12-20 |
| `minimax-m2.5` | 230,000,000,000 | 2026-02-12 |
| `minimax-m2.7` | 480,836,588,544 | 2026-03-18 |
| `minimax-m3` | 0 | 2026-06-01 |
| `ministral-3:14b` | 15,700,000,000 | 2025-12-02 |
| `ministral-3:3b` | 4,670,000,000 | 2025-12-02 |
| `ministral-3:8b` | 10,400,000,000 | 2025-12-02 |
| `mistral-large-3:675b` | 682,000,000,000 | 2025-12-02 |
| `nemotron-3-nano:30b` | 32,645,090,390 | 2025-12-15 |
| `nemotron-3-super` | 230,500,000,000 | 2026-03-11 |
| `nemotron-3-ultra` | 0 | 2026-06-04 |
| `qwen3-coder-next` | 81,800,000,000 | 2025-02-04 |
| `qwen3-coder:480b` | 510,492,157,952 | 2025-07-22 |
| `qwen3.5:397b` | 397,000,000,000 | 2026-02-16 |
| `rnj-1:8b` | 16,000,000,000 | 2025-12-09 |

### DeepSeek (api.deepseek.com/models)

Endpoint: `https://api.deepseek.com/models`

| Model | Context length | Max output | Cache hit / 1M tokens | Cache miss / 1M tokens | Output / 1M tokens | Concurrency limit |
| --- | --- | --- | --- | --- | --- | --- |
| `deepseek-v4-flash` | 1M | 384K | $0.0028 | $0.14 | $0.28 | 2500 |
| `deepseek-v4-pro` | 1M | 384K | $0.003625 | $0.435 | $0.87 | 500 |

DeepSeek's deprecated aliases: `deepseek-chat` and `deepseek-reasoner` (both
deprecated 2026-07-24 15:59 UTC). `deepseek-chat` maps to non-thinking mode of
`deepseek-v4-flash`; `deepseek-reasoner` maps to thinking mode.

### OpenCode Go (opencode.ai/zen/go/v1/models)

Endpoint: `https://opencode.ai/zen/go/v1/models`

| Model |
| --- |
| `deepseek-v4-flash` |
| `deepseek-v4-pro` |
| `glm-5` |
| `glm-5.1` |
| `glm-5.2` |
| `hy3-preview` |
| `kimi-k2.5` |
| `kimi-k2.6` |
| `kimi-k2.7-code` |
| `minimax-m2.5` |
| `minimax-m2.7` |
| `minimax-m3` |
| `mimo-v2-omni` |
| `mimo-v2-pro` |
| `mimo-v2.5` |
| `mimo-v2.5-pro` |
| `qwen3.5-plus` |
| `qwen3.6-plus` |
| `qwen3.7-max` |
| `qwen3.7-plus` |

OpenCode Go exposes all models under an OpenAI-compatible `/v1/models`
endpoint. The gateway routes them with LiteLLM's `openai/*` provider prefix
and adds `additional_drop_params: [reasoningSummary]` for the connections that
need it.

## Runtime configuration

| Setting | Value | Source in `gateway.config.yaml` |
| --- | --- | --- |
| Default model | `coder` | `router.default_model` |
| Cache TTL | 600 seconds | `router.cache_ttl_seconds` |
| Retry base delay | 0.2 seconds | `router.retry_base_delay` |
| Retry max delay | 2.0 seconds | `router.retry_max_delay` |
| Quota cooldown | 300 seconds | `router.quota_cooldown_seconds` |
| Request timeout | 120 seconds | `litellm.settings.request_timeout` |
| Retries | 3 | `litellm.settings.num_retries` |
| Drop unknown params | `true` | `litellm.settings.drop_params` |
| Cache backend | Redis via `REDIS_URL` | `litellm.cache` |

## Environment variables used by models

| Variable | Required by | Purpose |
| --- | --- | --- |
| `OLLAMA_API_BASE` | All `ollama-cloud.*` deployments | Ollama endpoint URL |
| `OLLAMA_API_KEY` | All `ollama-cloud.*` deployments | Ollama Cloud API key |
| `DEEPSEEK_API_KEY` | `deepseek-api.*` deployments | DeepSeek API key |
| `OPENCODE_GO_API_BASE` | All `opencode-go.*` deployments | OpenCode Go base URL |
| `OPENCODE_GO_API_KEY` | All `opencode-go.*` deployments | OpenCode Go API key |
| `VIRTUAL_KEY` | `gateway setup` CLI | Default LiteLLM virtual key written into client configs |

## OpenCode integration

Run `python3 src/scripts/gateway.py setup opencode --mode local-plugin
--catalog all --apply` to sync the gateway catalog into
`~/.config/opencode/opencode.json`. The default `local-plugin` mode installs a
first-party plugin that fetches the live catalog from `/v1/models?view=<catalog>`
at startup; `static` mode writes a snapshot of the current catalog into
`provider.gateway.models`. Use `--dry-run` to preview without writing.

## Updating this page

To refresh the provider catalog tables, source `src/.env` and run the fetch
script (or call the endpoints directly with `curl`). Replace the tables in the
"Live provider catalogs" section with the new responses.

# Provider Connections Live Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the gateway around provider registry metadata, active connections, combos, and live client discovery so local harnesses can use curated combos and experiment with direct provider models.

**Architecture:** `src/gateway.config.yaml` is the only human-edited source of truth. It defines provider registry metadata, active connections, curated combos, and client setup defaults. Runtime files for LiteLLM and the sticky router are generated adapters. `/v1/models` defaults to a live catalog built from active connections + registry metadata + combos, with query filters for narrower client views.

**Tech Stack:** Python 3, FastAPI, httpx, PyYAML, argparse, pytest, ruff, mypy. No new gateway runtime dependency. OpenCode setup may install a local JavaScript plugin file into the user's OpenCode config directory.

## Global Constraints

- No migration or backwards compatibility work. Replace the old shape.
- Optimize for the best local developer experience and model experimentation.
- Keep provider extensibility boring: YAML plus Python validation, no plugin loader.
- Keep LiteLLM as an execution adapter. Do not expose LiteLLM vocabulary as the primary user-facing model.
- `/v1/models` defaults to the full live catalog.
- Support filtered catalog views: `all`, `combos`, `connections`, and `registry`.
- First-class downstream client setup comes before Codex/Claude/Copilot account-backed upstream providers.
- Do not implement OAuth, browser cookie import, M365 WebSocket flows, compression, provider marketplaces, or many routing strategies.
- Initial combo strategies: `score` and `priority`.
- Quota v1 means recent 429/402/rate-limit cooldown, not a token ledger.
- Every non-trivial behavior gets a focused test. Run the full suite once at the end.

---

## Product Semantics

The gateway exposes three model kinds:

```text
combo             curated public model such as coder or planner
registry-model    model id from provider metadata, served by one or more active connections
connection-model  explicit qualified deployment id such as deepseek-api.deepseek-v4-pro
```

Routing behavior:

```text
combo id             -> use configured combo candidates and strategy
registry model id    -> use all active deployments that serve that model, scored by state
connection.model id  -> force that exact deployment
```

Fallback behavior:

```text
1. Resolve the requested model to an ordered deployment list.
2. Try deployments in order until one succeeds or the list is exhausted.
3. Retry only gateway/upstream failures that are likely recoverable.
4. Do not retry caller/request errors.
5. Record every attempt so future ordering learns from health, latency, quota cooldown, and active density.
```

Retryable failures:

```text
transport timeout/error
HTTP 408
HTTP 409 from provider overload/concurrency
HTTP 425
HTTP 429
HTTP 500, 502, 503, 504
status text containing quota/rate/overloaded/unavailable
```

Non-retryable failures:

```text
HTTP 400, 401, 403, 404, 422
invalid request payload
unsupported model when the requested id was a forced connection-model
client disconnect after streaming has started
```

Exhaustion behavior:

```text
all candidates fail before streaming starts -> return the last upstream error with gateway headers
all transport attempts fail -> return 502 gateway error with attempt summary
stream starts successfully -> do not switch providers mid-stream
```

Warm sessions:

```text
warm deployment still belongs to resolved candidate set and is not in quota cooldown -> try it first
warm deployment missing, disabled, or cooling down -> ignore it and use normal ordering
```

Session identity rules:

```text
warm sessions require X-Session-Id
no X-Session-Id -> no warm-session stickiness
session key includes X-Session-Id and caller auth fingerprint
stored session value includes requested model id, model kind, served deployment id, and timestamp
session TTL is router.cache_ttl_seconds
do not derive session ids from prompt content
```

Identity rules:

```text
provider id    -> lowercase slug: [a-z0-9][a-z0-9_-]*
connection id  -> lowercase slug: [a-z0-9][a-z0-9_-]*, no dots
combo id       -> lowercase slug: [a-z0-9][a-z0-9_-]*, no dots
model id       -> provider model id, exact opaque string, may contain dots/slashes
deployment id  -> <connection id>.<model id>, parsed by splitting on the first dot
```

Name collision rules:

```text
combo ids and registry model ids share the unqualified /v1/models namespace
combo id == registry model id -> config error
combo id that starts with "<connection id>." -> config error
registry model id that starts with "<connection id>." -> config error
deployment ids are always qualified and may coexist with their registry model id
model ids are case-sensitive; do not lowercase requested model ids
```

Configured vs active deployments:

```text
configured deployment -> enabled connection + provider registry model; may be generated into LiteLLM config
active deployment     -> configured deployment whose required env vars are present at router runtime
inactive deployment   -> missing required env, disabled connection, missing registry model, or failed validation
```

Catalog inclusion:

```text
/v1/models includes active deployments only
combos with zero active candidates are hidden from /v1/models
registry models with zero active deployments are hidden from /v1/models
connection-model ids are shown only for active deployments
doctor/explain show inactive entries with reasons
```

Request resolution:

```text
requested id matches combo           -> combo resolution
requested id matches deployment id   -> forced connection-model resolution
requested id matches registry model  -> registry-model resolution
requested id missing/null            -> default_model resolution
requested id unknown                 -> 404 gateway_model_not_found, no default fallback
default_model unavailable            -> 503 with diagnostic JSON, no silent fallback
```

Resolution outcomes:

```text
kind=combo              ordered_deployments comes from combo candidates after active filtering and strategy ordering
kind=registry-model     ordered_deployments comes from active deployments serving that registry model
kind=connection-model   ordered_deployments is exactly one active deployment
kind=unavailable        ordered_deployments is empty and request returns 503
kind=not-found          ordered_deployments is empty and request returns 404
```

Strategy rules:

```text
priority strategy -> keep configured/generated order after removing inactive deployments; deployments in quota cooldown move to the end
score strategy    -> sort by score descending after removing inactive deployments; stable tie-breaker keeps configured/generated order
connection-model  -> forced exact deployment; no alternative provider fallback
registry-model    -> uses default scoring weights unless configured per registry model in the future
```

Default scoring weights:

```yaml
health: 0.30
latency: 0.20
quota: 0.15
stability: 0.15
connection_density: 0.10
priority: 0.10
```

Scoring weight rules:

```text
missing scoring block -> use default scoring weights
partial scoring block -> merge provided keys over defaults
weights do not need to sum to 1.0
each weight must be numeric and 0.0 <= weight <= 1.0
priority strategy ignores scoring weights
```

Runtime signal rules:

```text
state is process-local and resets on router restart
active_count increments before an attempt and decrements in finally
latency uses EWMA: first latency is stored as-is, then ewma = 0.2 * latest + 0.8 * previous
health uses process-lifetime provider-attempt success ratio
successful 2xx attempts increment successes and attempts
retryable failures increment attempts and retryable failures
non-retryable caller errors do not change provider health
quota cooldown starts on 402, 429, or status text containing quota/rate
quota cooldown lasts router.quota_cooldown_seconds
```

Catalog metadata rules:

```text
capabilities are unioned across active deployments for a registry model
context_length is the minimum known context length across active deployments
pricing is the minimum known input/output price across active deployments
provider/connection/deployment lists are sorted for stable output
unknown metadata is omitted, not guessed
```

Registry metadata rules:

```text
capabilities use lowercase slugs
known capabilities: chat, coding, reasoning, vision, fast, cheap, local, free, tool-use, json, streaming
custom capabilities are allowed if they are lowercase slugs and are passed through unchanged
context_length is an integer token count
pricing values are USD per token
display_name is optional and never used as an id
```

Catalog ordering rules:

```text
all view         -> combos first in config order, then registry models sorted by id, then connection-models sorted by id
combos view      -> combos in config order
registry view    -> registry models sorted by id
connections view -> connection-models sorted by id
```

Secret handling:

```text
API keys and bearer tokens are never returned from /v1/models, /dashboard/api/live, doctor, explain, logs, or errors
doctor may print env var names and whether they are present
setup --dry-run prints $ENV_NAME placeholders by default
setup --apply requires a concrete key from --api-key or env
```

Auth and virtual-key rules:

```text
router forwards the caller Authorization header to LiteLLM unchanged
router does not replace caller auth with the LiteLLM master key
LiteLLM sees the rewritten internal deployment id
LiteLLM virtual-key allowlists must include deployment ids for models the key may use
combos and registry model ids are gateway catalog ids, not LiteLLM allowlist ids in v1
/v1/models is gateway-wide and not filtered by virtual key in v1
LiteLLM 401/403 responses are surfaced and never trigger provider fallback
doctor documents this and prints the active deployment ids to use in LiteLLM allowlists
```

Client setup write policy:

```text
setup defaults to --dry-run
setup --apply always writes a timestamped .bak before modifying an existing file
JSON client configs must parse as JSON before writing; invalid JSON aborts with no write
JSON client configs preserve unrelated top-level keys
managed JSON lives under an "agent-ai-gateway" key unless the client requires a known provider block
TOML/text client configs update only the managed block between "# agent-ai-gateway:start" and "# agent-ai-gateway:end"
if no managed TOML/text block exists, append one at the end
client setup never writes master keys; it writes virtual keys only
```

Response headers:

```text
X-Gateway-Requested-Model     original request model after defaulting
X-Gateway-Model-Kind          combo | registry-model | connection-model | unavailable | not-found
X-Gateway-Served-Deployment   deployment id that produced a successful response
X-Gateway-Fallback-Count      number of failed deployments before success
X-Gateway-Attempted-Models    comma-separated deployment ids attempted before response
```

Error response shapes:

```json
{
  "error": {
    "type": "gateway_model_not_found",
    "message": "Model 'typo-model' is not in the live gateway catalog.",
    "model": "typo-model"
  }
}
```

```json
{
  "error": {
    "type": "gateway_no_active_deployment",
    "message": "No active deployments are available for model 'kimi-k2.7-code'.",
    "model": "kimi-k2.7-code",
    "kind": "registry-model",
    "inactive_reasons": {"ollama-local.kimi-k2.7-code": ["missing env OLLAMA_API_KEY"]}
  }
}
```

```json
{
  "error": {
    "type": "gateway_catalog_view_invalid",
    "message": "Unknown catalog view 'bad'. Use all, combos, registry, or connections.",
    "view": "bad"
  }
}
```

```json
{
  "error": {
    "type": "gateway_upstream_exhausted",
    "message": "All candidate deployments failed before a response stream started.",
    "model": "coder",
    "attempted": ["ollama-local.kimi-k2.7-code", "deepseek-api.deepseek-v4-pro"],
    "last_status": 503
  }
}
```

Catalog behavior:

```text
GET /v1/models                  -> all live catalog entries
GET /v1/models?view=all         -> all live catalog entries
GET /v1/models?view=combos      -> only curated combos
GET /v1/models?view=registry    -> registry model ids served by active connections
GET /v1/models?view=connections -> explicit qualified connection-model ids
```

Client onboarding follows the OmniRoute setup shape:

```text
1. resolve local or remote gateway target
2. fetch the live model catalog from /v1/models?view=<catalog> when the client needs a model list
3. render the smallest client-specific config that points at the gateway
4. dry-run by default
5. apply idempotently, preserving unrelated config
6. provide a doctor/status check that confirms install, config, endpoint, and catalog visibility
```

Client setup defaults to `view=all`, but each client can choose a smaller catalog:

```bash
python3 src/scripts/gateway.py setup codex --catalog all --dry-run
python3 src/scripts/gateway.py setup opencode --mode local-plugin --catalog combos --apply
```

Setup target resolution:

```text
--remote URL        -> configure the client against that gateway URL
--base-url URL      -> synonym for --remote
no URL flag         -> use clients.base_url from gateway.config.yaml
--api-key KEY       -> literal virtual key to write only when the client cannot reference env
--api-key-env NAME  -> write the client's env substitution syntax when supported
no key flag         -> use clients.api_key_env from gateway.config.yaml
```

API key write policy:

```text
Prefer env/file references over literal secrets whenever the target client supports them.
OpenCode supports {env:NAME}, so OpenCode plugin/static config uses {env:VIRTUAL_KEY}.
Codex and Claude Code setup must not write LiteLLM master keys.
Dry-run prints placeholders only.
Apply may write a literal key only when --api-key is passed explicitly and the renderer marks literal keys as supported.
```

OpenCode onboarding:

```text
OpenCode default setup mode is local-plugin, not static.
local-plugin mode copies our first-party plugin to ~/.config/opencode/plugins/agent-ai-gateway/.
local-plugin mode writes only a plugin entry plus default model settings to opencode.json.
local-plugin mode fetches /v1/models?view=<catalog> at OpenCode startup and on TTL refresh.
local-plugin mode must not enumerate gateway models into opencode.json.
static mode writes the current live catalog snapshot for environments where plugins are not wanted.
static mode must enumerate the selected catalog view into provider.gateway.models.
static mode is explicitly second-class because it can drift behind provider/connection changes.
OpenCode model ids appear to users as gateway/<catalog-id>; the gateway receives <catalog-id>.
```

OpenCode setup commands:

```bash
python3 src/scripts/gateway.py setup opencode --mode local-plugin --catalog all --dry-run
python3 src/scripts/gateway.py setup opencode --mode local-plugin --catalog all --apply
python3 src/scripts/gateway.py setup opencode --mode static --catalog combos --apply
python3 src/scripts/gateway.py doctor opencode
```

OpenCode plugin entry:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "gateway/coder",
  "small_model": "gateway/coder-fast",
  "plugin": [
    [
      "./plugins/agent-ai-gateway/index.js",
      {
        "providerId": "gateway",
        "displayName": "Agent AI Gateway",
        "baseURL": "http://localhost:4100/v1",
        "apiKey": "{env:VIRTUAL_KEY}",
        "catalog": "all",
        "modelCacheTtl": 300000
      }
    ]
  ]
}
```

OpenCode static provider shape:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "gateway/coder",
  "small_model": "gateway/coder-fast",
  "provider": {
    "gateway": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Agent AI Gateway",
      "options": {
        "baseURL": "http://localhost:4100/v1",
        "apiKey": "{env:VIRTUAL_KEY}"
      },
      "models": {
        "coder": { "name": "coder" },
        "coder-fast": { "name": "coder-fast" }
      }
    }
  }
}
```

---

## File Structure

**Create**

- `src/router/gateway_config.py`
  Loads and validates human YAML. Expands provider registry metadata and active connections into internal deployments.

- `src/router/live_catalog.py`
  Builds `/v1/models` entries from combos, registry metadata, active connections, and routing state.

- `src/router/routing_state.py`
  Tracks in-memory runtime signals and orders candidate deployments.

- `src/scripts/gateway.py`
  One local CLI front door: `generate`, `doctor`, `explain`, `models`, and `setup`.

- `src/clients/opencode_plugin/index.js`
  Minimal first-party OpenCode plugin installed by `gateway.py setup opencode --mode local-plugin`. Fetches the live gateway catalog and emits an OpenAI-compatible provider.

- `src/router/tests/test_gateway_config.py`
  Tests config loading, provider registry expansion, validation, and generated deployment ids.

- `src/router/tests/test_live_catalog.py`
  Tests default full catalog, filtered views, registry model grouping, and qualified connection models.

- `src/router/tests/test_routing_state.py`
  Tests scoring, cooldowns, latency preference, and active connection density.

- `src/router/tests/test_gateway_cli.py`
  Tests CLI model rendering and client setup dry-run/apply behavior with temporary paths.

**Modify**

- `src/gateway.config.yaml`
  Replace `models` and `aliases` with `providers`, `connections`, `combos`, and `clients`.

- `src/scripts/generate_configs.py`
  Generate LiteLLM and router YAML from `GatewayCatalog`.

- `src/router/config.py`
  Load the new generated router config shape.

- `src/router/routing.py`
  Resolve requested model ids as combo, registry-model, or connection-model.

- `src/router/main.py`
  Serve live `/v1/models` and send internal deployment ids to LiteLLM.

- `src/router/metrics.py`
  Expose connection attempt counters and latency snapshots already tracked by routing state.

- `src/router/dashboard.py`
  Add catalog and routing-state JSON to `/dashboard/api/live`.

- `README.md`, `src/README.md`, `docs/models.md`
  Document the new model catalog and commands.

**Delete**

- `src/scripts/generate_opencode_config.py` after `src/scripts/gateway.py setup opencode` replaces it.

---

## Target Config Shape

Use mappings for ids so users do not repeat `id` fields everywhere.

```yaml
router:
  default_model: coder
  cache_ttl_seconds: 600
  retry_base_delay: 0.2
  retry_max_delay: 2.0
  quota_cooldown_seconds: 300

providers:
  ollama:
    adapter: litellm
    litellm_model_prefix: ollama_chat
    api_base_env: OLLAMA_API_BASE
    api_key_env: OLLAMA_API_KEY
    registry:
      models:
        deepseek-v4-flash:
          display_name: DeepSeek V4 Flash
          capabilities: [chat, coding, fast]
          context_length: 128000
        kimi-k2.7-code:
          display_name: Kimi K2.7 Code
          capabilities: [chat, coding]
          context_length: 128000

  deepseek:
    adapter: litellm
    litellm_model_prefix: deepseek
    api_key_env: DEEPSEEK_API_KEY
    registry:
      models:
        deepseek-v4-flash:
          display_name: DeepSeek V4 Flash
          capabilities: [chat, coding, fast]
          context_length: 128000
          pricing:
            input_cost_per_token: 0.00000014
            output_cost_per_token: 0.00000028
        deepseek-v4-pro:
          display_name: DeepSeek V4 Pro
          capabilities: [chat, coding, reasoning]
          context_length: 128000
          pricing:
            input_cost_per_token: 0.00000028
            output_cost_per_token: 0.00000056

  opencode-go:
    adapter: litellm
    litellm_model_prefix: openai
    api_base_env: OPENCODE_GO_API_BASE
    api_key_env: OPENCODE_GO_API_KEY
    drop_params:
      - reasoningSummary
    registry:
      models:
        deepseek-v4-flash:
          display_name: DeepSeek V4 Flash
          capabilities: [chat, coding, fast]
          context_length: 128000
        kimi-k2.7-code:
          display_name: Kimi K2.7 Code
          capabilities: [chat, coding]
          context_length: 128000

connections:
  ollama-local:
    provider: ollama
    enabled: true
    priority: 10
    stability: 0.85
    max_concurrent: 8
    models: all

  deepseek-api:
    provider: deepseek
    enabled: true
    priority: 30
    stability: 0.9
    max_concurrent: 20
    models:
      - deepseek-v4-flash
      - deepseek-v4-pro

  opencode-go:
    provider: opencode-go
    enabled: true
    priority: 40
    stability: 0.7
    max_concurrent: 8
    models: all

combos:
  coder:
    task: build
    strategy: score
    candidates:
      - connection: ollama-local
        model: kimi-k2.7-code
      - connection: deepseek-api
        model: deepseek-v4-pro
      - connection: opencode-go
        model: kimi-k2.7-code
    scoring:
      health: 0.30
      latency: 0.20
      quota: 0.15
      stability: 0.15
      connection_density: 0.10
      priority: 0.10

  coder-fast:
    task: quick-build
    strategy: score
    candidates:
      - connection: ollama-local
        model: deepseek-v4-flash
      - connection: deepseek-api
        model: deepseek-v4-flash
      - connection: opencode-go
        model: deepseek-v4-flash

clients:
  base_url: http://localhost:4100/v1
  api_key_env: VIRTUAL_KEY
  default_model: coder
  default_catalog: all
  targets:
    codex:
      model: coder
      catalog: all
      paths:
        - ~/.codex/config.toml
        - ~/.codex/codex.toml
    claude-code:
      model: coder
      catalog: all
      paths:
        - ~/.claude/settings.json
    opencode:
      model: coder
      small_model: coder-fast
      catalog: all
      mode: local-plugin
      provider_id: gateway
      plugin_dir: ~/.config/opencode/plugins/agent-ai-gateway
      model_cache_ttl_ms: 300000
      paths:
        - ~/.config/opencode/opencode.json
    pi:
      model: coder
      catalog: all
      paths:
        - ~/.config/pi/settings.json
```

Internal deployment ids use `connection.model`:

```text
ollama-local.kimi-k2.7-code
deepseek-api.deepseek-v4-pro
```

Public catalog ids include combos, registry model ids, and qualified connection model ids:

```text
coder
coder-fast
deepseek-v4-flash
deepseek-v4-pro
ollama-local.kimi-k2.7-code
deepseek-api.deepseek-v4-pro
```

---

### Task 1: Gateway Catalog and Provider Registry

**Files:**

- Create: `src/router/gateway_config.py`
- Create: `src/router/tests/test_gateway_config.py`
- Modify: `src/gateway.config.yaml`

**Interfaces:**

- Produces:
  - `load_gateway_catalog(path: Path) -> GatewayCatalog`
  - `expand_gateway_config(data: Mapping[str, Any]) -> GatewayCatalog`
  - `GatewayCatalog.providers: dict[str, Provider]`
  - `GatewayCatalog.connections: dict[str, Connection]`
  - `GatewayCatalog.deployments: dict[str, Deployment]`
  - `GatewayCatalog.combos: dict[str, Combo]`
  - `GatewayConfigError`

- [ ] **Step 1: Write failing catalog tests**

Add `src/router/tests/test_gateway_config.py` with tests equivalent to:

```python
import pytest

from src.router.gateway_config import GatewayConfigError, expand_gateway_config


def minimal_config():
    return {
        "providers": {
            "ollama": {
                "adapter": "litellm",
                "litellm_model_prefix": "ollama_chat",
                "api_base_env": "OLLAMA_API_BASE",
                "api_key_env": "OLLAMA_API_KEY",
                "registry": {
                    "models": {
                        "kimi-k2.7-code": {
                            "display_name": "Kimi K2.7 Code",
                            "capabilities": ["chat", "coding"],
                            "context_length": 128000,
                        }
                    }
                },
            },
            "deepseek": {
                "adapter": "litellm",
                "litellm_model_prefix": "deepseek",
                "api_key_env": "DEEPSEEK_API_KEY",
                "registry": {
                    "models": {
                        "deepseek-v4-pro": {
                            "display_name": "DeepSeek V4 Pro",
                            "capabilities": ["chat", "coding", "reasoning"],
                        }
                    }
                },
            },
        },
        "connections": {
            "ollama-local": {
                "provider": "ollama",
                "enabled": True,
                "priority": 10,
                "max_concurrent": 8,
                "models": "all",
            },
            "deepseek-api": {
                "provider": "deepseek",
                "enabled": True,
                "priority": 30,
                "models": ["deepseek-v4-pro"],
            },
        },
        "combos": {
            "coder": {
                "strategy": "score",
                "candidates": [
                    {"connection": "ollama-local", "model": "kimi-k2.7-code"},
                    {"connection": "deepseek-api", "model": "deepseek-v4-pro"},
                ],
            }
        },
    }


def test_expands_active_connections_into_deployments():
    catalog = expand_gateway_config(minimal_config())
    assert sorted(catalog.deployments) == [
        "deepseek-api.deepseek-v4-pro",
        "ollama-local.kimi-k2.7-code",
    ]
    assert catalog.deployments["ollama-local.kimi-k2.7-code"].litellm_model == (
        "ollama_chat/kimi-k2.7-code"
    )
    assert catalog.deployments["ollama-local.kimi-k2.7-code"].capabilities == (
        "chat",
        "coding",
    )


def test_disabled_connections_do_not_create_deployments():
    config = minimal_config()
    config["connections"]["ollama-local"]["enabled"] = False
    catalog = expand_gateway_config(config)
    assert "ollama-local.kimi-k2.7-code" not in catalog.deployments


def test_rejects_combo_candidate_for_missing_connection():
    config = minimal_config()
    config["combos"]["coder"]["candidates"][0]["connection"] = "missing"
    with pytest.raises(GatewayConfigError, match="unknown connection"):
        expand_gateway_config(config)


def test_rejects_combo_registry_model_id_collision():
    config = minimal_config()
    config["combos"]["kimi-k2.7-code"] = config["combos"].pop("coder")
    with pytest.raises(GatewayConfigError, match="collides with registry model"):
        expand_gateway_config(config)


def test_rejects_dot_in_connection_id():
    config = minimal_config()
    config["connections"]["ollama.local"] = config["connections"].pop("ollama-local")
    config["combos"]["coder"]["candidates"][0]["connection"] = "ollama.local"
    with pytest.raises(GatewayConfigError, match="connection id"):
        expand_gateway_config(config)


def test_model_id_is_exact_and_not_lowercased():
    config = minimal_config()
    config["providers"]["ollama"]["registry"]["models"] = {
        "Model.With.Case": {"display_name": "Case Model", "capabilities": ["chat"]}
    }
    config["connections"]["ollama-local"]["models"] = ["Model.With.Case"]
    config["combos"]["coder"]["candidates"][0]["model"] = "Model.With.Case"
    catalog = expand_gateway_config(config)
    assert "ollama-local.Model.With.Case" in catalog.deployments
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
python3 -m pytest src/router/tests/test_gateway_config.py -q
```

Expected: fail because `src/router/gateway_config.py` does not exist.

- [ ] **Step 3: Implement `gateway_config.py`**

Use frozen dataclasses. Keep the model small:

```python
@dataclass(frozen=True)
class Deployment:
    id: str
    connection_id: str
    provider_id: str
    model_id: str
    upstream: str
    litellm_model: str
    display_name: str | None = None
    capabilities: tuple[str, ...] = ()
    context_length: int | None = None
    api_base_env: str | None = None
    api_key_env: str | None = None
    drop_params: tuple[str, ...] = ()
    priority: int = 100
    stability: float = 0.8
    max_concurrent: int | None = None
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None
```

Validation rules:

- provider adapter must be `litellm`
- provider `litellm_model_prefix` is required
- provider ids, connection ids, and combo ids must be lowercase slugs
- connection ids and combo ids must not contain `.`
- combo ids must not collide with registry model ids or deployment id prefixes
- model ids are exact opaque strings and must not be lowercased
- provider registry has at least one model if any connection uses `models: all`
- every enabled connection references an existing provider
- every enabled connection model exists in provider registry
- every combo candidate references an enabled connection/model pair
- combo strategy is `score` or `priority`
- scoring weights are numeric and between `0.0` and `1.0`
- router default model exists in combos, registry-model ids, or connection-model ids

- [ ] **Step 4: Replace `src/gateway.config.yaml`**

Use the target config shape above. Include existing local providers and public combos:

```text
providers: ollama, deepseek, opencode-go
combos: explorer, planner, coder, coder-fast, vision
```

- [ ] **Step 5: Re-run tests**

Run:

```bash
python3 -m pytest src/router/tests/test_gateway_config.py -q
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add src/router/gateway_config.py src/router/tests/test_gateway_config.py src/gateway.config.yaml
git commit -m "feat: add gateway provider catalog"
```

---

### Task 2: Generated Runtime Configs From Configured Deployments

**Files:**

- Modify: `src/scripts/generate_configs.py`
- Modify: `src/router/config.py`
- Modify: `src/router/tests/test_gateway_config_generator.py`
- Modify: `src/router/tests/test_config.py`

**Interfaces:**

- Consumes: `GatewayCatalog` from Task 1.
- Produces:
  - LiteLLM config with one entry per configured enabled deployment.
  - Router config with combos, deployments, required env names, registry model grouping, and catalog metadata.

- [ ] **Step 1: Write failing generator tests**

Update tests so `render_litellm_config(config)` emits configured internal deployment ids:

```python
model_names = [entry["model_name"] for entry in render_litellm_config(config)["model_list"]]
assert "ollama-local.kimi-k2.7-code" in model_names
assert "deepseek-api.deepseek-v4-pro" in model_names
assert "coder" not in model_names
```

Update router config expectations:

```python
router_config = render_router_config(config)
assert router_config["default_model"] == "coder"
assert router_config["combos"]["coder"]["candidates"][0] == "ollama-local.kimi-k2.7-code"
assert router_config["deployments"]["ollama-local.kimi-k2.7-code"]["provider"] == "ollama"
assert router_config["deployments"]["ollama-local.kimi-k2.7-code"]["required_env"] == [
    "OLLAMA_API_BASE",
    "OLLAMA_API_KEY",
]
assert "ollama-local.kimi-k2.7-code" in router_config["registry_models"]["kimi-k2.7-code"]
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python3 -m pytest src/router/tests/test_gateway_config_generator.py src/router/tests/test_config.py -q
```

Expected: fail because code still expects `models` and `aliases`.

- [ ] **Step 3: Render LiteLLM config from configured deployments**

`render_litellm_config()` should loop over `catalog.deployments.values()` and create one LiteLLM entry per configured enabled deployment. Runtime active/inactive filtering happens in the router after env checks:

```python
{
    "model_name": deployment.id,
    "litellm_params": {
        "model": deployment.litellm_model,
        "api_base": _env_ref(deployment.api_base_env) if deployment.api_base_env else None,
        "api_key": _env_ref(deployment.api_key_env) if deployment.api_key_env else None,
    },
    "model_info": {
        "provider": deployment.provider_id,
        "connection": deployment.connection_id,
        "model": deployment.model_id,
    },
}
```

Drop `None` values before writing YAML.

- [ ] **Step 4: Render router config for catalog and routing**

Generated router config should include:

```yaml
default_model: coder
quota_cooldown_seconds: 300
combos:
  coder:
    strategy: score
    candidates:
      - ollama-local.kimi-k2.7-code
deployments:
  ollama-local.kimi-k2.7-code:
    provider: ollama
    connection: ollama-local
    model: kimi-k2.7-code
    required_env:
      - OLLAMA_API_BASE
      - OLLAMA_API_KEY
registry_models:
  kimi-k2.7-code:
    - ollama-local.kimi-k2.7-code
catalog:
  default_view: all
```

- [ ] **Step 5: Load new router config**

Update `RouteConfig` so it stores:

```python
default_model: str
combos: dict[str, ComboRuntime]
deployments: dict[str, DeploymentRuntime]
registry_models: dict[str, list[str]]
quota_cooldown_seconds: int = 300
catalog_default_view: str = "all"
```

- [ ] **Step 6: Regenerate runtime YAML**

Run:

```bash
python3 src/scripts/generate_configs.py
```

Expected:

- `src/litellm.config.yaml` contains `ollama-local.kimi-k2.7-code`
- `src/router/router_config.yaml` contains `combos`, `deployments`, and `registry_models`

- [ ] **Step 7: Re-run tests**

Run:

```bash
python3 -m pytest src/router/tests/test_gateway_config_generator.py src/router/tests/test_config.py -q
```

Expected: pass.

- [ ] **Step 8: Commit**

```bash
git add src/scripts/generate_configs.py src/router/config.py src/router/tests/test_gateway_config_generator.py src/router/tests/test_config.py src/litellm.config.yaml src/router/router_config.yaml
git commit -m "feat: generate runtime configs from live catalog"
```

---

### Task 3: Live Model Catalog API

**Files:**

- Create: `src/router/live_catalog.py`
- Create: `src/router/tests/test_live_catalog.py`
- Modify: `src/router/main.py`
- Modify: `src/router/dashboard.py`
- Modify: `src/router/tests/test_app.py`
- Modify: `src/router/tests/test_dashboard.py`

**Interfaces:**

- Produces:
  - `deployment_is_active(deployment: DeploymentRuntime, env: Mapping[str, str]) -> tuple[bool, list[str]]`
  - `active_deployment_ids(config: RouteConfig, env: Mapping[str, str]) -> set[str]`
  - `build_live_model_catalog(config: RouteConfig, view: str = "all", env: Mapping[str, str] | None = None) -> list[dict[str, Any]]`
  - `/v1/models` default full catalog.
  - `/v1/models?view=combos|registry|connections|all`.

- [ ] **Step 1: Write failing live catalog tests**

Add `src/router/tests/test_live_catalog.py` with tests equivalent to:

```python
def test_all_view_includes_combos_registry_and_connection_models(route_config):
    env = {"OLLAMA_API_BASE": "http://ollama", "OLLAMA_API_KEY": "x", "DEEPSEEK_API_KEY": "x"}
    ids = [entry["id"] for entry in build_live_model_catalog(route_config, view="all", env=env)]
    assert "coder" in ids
    assert "kimi-k2.7-code" in ids
    assert "ollama-local.kimi-k2.7-code" in ids
```

```python
def test_combo_view_only_includes_combos(route_config):
    env = {"OLLAMA_API_BASE": "http://ollama", "OLLAMA_API_KEY": "x", "DEEPSEEK_API_KEY": "x"}
    entries = build_live_model_catalog(route_config, view="combos", env=env)
    assert {entry["gateway"]["kind"] for entry in entries} == {"combo"}
```

```python
def test_registry_model_groups_active_deployments(route_config):
    env = {"OLLAMA_API_BASE": "http://ollama", "OLLAMA_API_KEY": "x", "OPENCODE_GO_API_BASE": "http://go", "OPENCODE_GO_API_KEY": "x"}
    entry = next(e for e in build_live_model_catalog(route_config, env=env) if e["id"] == "kimi-k2.7-code")
    assert entry["gateway"]["kind"] == "registry-model"
    assert "ollama-local.kimi-k2.7-code" in entry["gateway"]["deployments"]
```

```python
def test_missing_required_env_hides_deployment(route_config):
    entries = build_live_model_catalog(route_config, view="connections", env={})
    ids = [entry["id"] for entry in entries]
    assert "ollama-local.kimi-k2.7-code" not in ids
```

```python
def test_metadata_aggregation_is_deterministic(route_config):
    env = {"OLLAMA_API_BASE": "http://ollama", "OLLAMA_API_KEY": "x", "OPENCODE_GO_API_BASE": "http://go", "OPENCODE_GO_API_KEY": "x"}
    entry = next(e for e in build_live_model_catalog(route_config, env=env) if e["id"] == "kimi-k2.7-code")
    assert entry["gateway"]["providers"] == ["ollama", "opencode-go"]
    assert entry["gateway"]["connections"] == ["ollama-local", "opencode-go"]
    assert entry["gateway"]["capabilities"] == ["chat", "coding"]
```

```python
def test_all_catalog_order_is_stable(route_config):
    env = {"OLLAMA_API_BASE": "http://ollama", "OLLAMA_API_KEY": "x", "DEEPSEEK_API_KEY": "x", "OPENCODE_GO_API_BASE": "http://go", "OPENCODE_GO_API_KEY": "x"}
    ids = [entry["id"] for entry in build_live_model_catalog(route_config, env=env)]
    assert ids.index("coder") < ids.index("kimi-k2.7-code")
    assert ids.index("kimi-k2.7-code") < ids.index("ollama-local.kimi-k2.7-code")
```

```python
def test_rejects_unknown_view(route_config):
    with pytest.raises(ValueError, match="catalog view"):
        build_live_model_catalog(route_config, view="unknown", env={})
```

- [ ] **Step 2: Write failing `/v1/models` tests**

Update app tests:

```python
ids = [item["id"] for item in client.get("/v1/models").json()["data"]]
assert "coder" in ids
assert "kimi-k2.7-code" in ids
assert "ollama-local.kimi-k2.7-code" in ids
```

Add a filtered view test:

```python
ids = [item["id"] for item in client.get("/v1/models?view=combos").json()["data"]]
assert "coder" in ids
assert "ollama-local.kimi-k2.7-code" not in ids
```

Add an invalid view test:

```python
response = client.get("/v1/models?view=bad")
assert response.status_code == 400
assert response.json()["error"]["type"] == "gateway_catalog_view_invalid"
```

- [ ] **Step 3: Run tests to verify failure**

Run:

```bash
python3 -m pytest src/router/tests/test_live_catalog.py src/router/tests/test_app.py::test_models_proxies_to_litellm -q
```

Expected: fail because `live_catalog.py` does not exist and models still proxy LiteLLM.

- [ ] **Step 4: Implement catalog entries**

Entry shape:

```json
{
  "id": "kimi-k2.7-code",
  "object": "model",
  "owned_by": "gateway",
  "gateway": {
    "kind": "registry-model",
    "model": "kimi-k2.7-code",
    "providers": ["ollama", "opencode-go"],
    "connections": ["ollama-local", "opencode-go"],
    "deployments": ["ollama-local.kimi-k2.7-code", "opencode-go.kimi-k2.7-code"],
    "capabilities": ["chat", "coding"],
    "context_length": 128000
  }
}
```

Combo entries include `kind: combo`, `strategy`, `task`, and candidates. Connection-model entries include `kind: connection-model`, provider, connection, model, capabilities, and context length.

- [ ] **Step 5: Replace `/v1/models` handler**

The default handler should build from `RouteConfig`, not LiteLLM:

```python
view = request.query_params.get("view", config.catalog_default_view)
data = build_live_model_catalog(config, app.state.routing_state, view=view)
return {"object": "list", "data": data}
```

`build_live_model_catalog(..., env=None)` uses `os.environ`; tests pass explicit env mappings.

Unknown view returns `400` with a clear JSON error.

- [ ] **Step 6: Add dashboard catalog JSON**

Expose catalog summary under `/dashboard/api/live`:

```python
payload["catalog"]["counts"]["combos"]
payload["catalog"]["counts"]["registry_models"]
payload["catalog"]["counts"]["connection_models"]
```

- [ ] **Step 7: Re-run focused tests**

Run:

```bash
python3 -m pytest src/router/tests/test_live_catalog.py src/router/tests/test_app.py src/router/tests/test_dashboard.py -q
```

Expected: pass.

- [ ] **Step 8: Commit**

```bash
git add src/router/live_catalog.py src/router/main.py src/router/dashboard.py src/router/tests/test_live_catalog.py src/router/tests/test_app.py src/router/tests/test_dashboard.py
git commit -m "feat: expose live model catalog"
```

---

### Task 4: Routing for Combos, Registry Models, and Direct Deployments

**Files:**

- Create: `src/router/routing_state.py`
- Create: `src/router/tests/test_routing_state.py`
- Modify: `src/router/routing.py`
- Modify: `src/router/main.py`
- Modify: `src/router/metrics.py`
- Modify: `src/router/dashboard.py`
- Modify: `src/router/tests/test_routing.py`
- Modify: `src/router/tests/test_app.py`
- Modify: `src/router/tests/test_streaming.py`
- Modify: `src/router/tests/test_dashboard.py`

**Interfaces:**

- Produces:
  - `GatewayRoutingState.start_attempt(deployment_id: str) -> AttemptToken`
  - `GatewayRoutingState.finish_attempt(token: AttemptToken, *, status: int | str, latency_ms: float) -> None`
  - `GatewayRoutingState.order_deployments(deployment_ids: Sequence[str], deployments: Mapping[str, DeploymentRuntime], weights: ScoringWeights | None, now: float) -> list[str]`
  - `resolve_model_request(model: str, config: RouteConfig, state: GatewayRoutingState, now: float, env: Mapping[str, str] | None = None) -> ResolvedModel`

- [ ] **Step 1: Write failing routing-state tests**

Add tests for the real signals:

```python
def test_recent_quota_status_moves_candidate_last():
    state = GatewayRoutingState(quota_cooldown_seconds=300)
    token = state.start_attempt("a")
    state.finish_attempt(token, status=429, latency_ms=10)
    ordered = state.order_deployments(["a", "b"], deployments, weights, now=time.time())
    assert ordered[-1] == "a"
```

```python
def test_lower_latency_wins_when_health_equal():
    state = GatewayRoutingState()
    state.record_latency("a", 900)
    state.record_latency("b", 100)
    assert state.order_deployments(["a", "b"], deployments, weights, now=1000.0)[0] == "b"
```

```python
def test_full_connection_density_penalizes_candidate():
    state = GatewayRoutingState()
    state.set_active_for_test("a", active=8)
    state.set_active_for_test("b", active=0)
    deployments["a"].max_concurrent = 8
    deployments["b"].max_concurrent = 8
    assert state.order_deployments(["a", "b"], deployments, weights, now=1000.0)[0] == "b"
```

```python
def test_missing_scoring_weights_use_defaults():
    state = GatewayRoutingState()
    ordered = state.order_deployments(["a", "b"], deployments, weights=None, now=1000.0)
    assert ordered == ["a", "b"]
```

- [ ] **Step 2: Write failing model-resolution tests**

Add tests in `src/router/tests/test_routing.py`:

```python
def test_combo_resolves_to_combo_candidates(route_config):
    env = {"OLLAMA_API_BASE": "http://ollama", "OLLAMA_API_KEY": "x", "DEEPSEEK_API_KEY": "x"}
    resolved = resolve_model_request("coder", route_config, GatewayRoutingState(), now=1000.0, env=env)
    assert resolved.kind == "combo"
    assert resolved.ordered_deployments[0].endswith(".kimi-k2.7-code")
```

```python
def test_registry_model_resolves_to_all_active_deployments(route_config):
    env = {"OLLAMA_API_BASE": "http://ollama", "OLLAMA_API_KEY": "x", "OPENCODE_GO_API_BASE": "http://go", "OPENCODE_GO_API_KEY": "x"}
    resolved = resolve_model_request("kimi-k2.7-code", route_config, GatewayRoutingState(), now=1000.0, env=env)
    assert resolved.kind == "registry-model"
    assert "ollama-local.kimi-k2.7-code" in resolved.ordered_deployments
```

```python
def test_connection_model_forces_one_deployment(route_config):
    env = {"OLLAMA_API_BASE": "http://ollama", "OLLAMA_API_KEY": "x"}
    resolved = resolve_model_request(
        "ollama-local.kimi-k2.7-code", route_config, GatewayRoutingState(), now=1000.0, env=env
    )
    assert resolved.kind == "connection-model"
    assert resolved.ordered_deployments == ["ollama-local.kimi-k2.7-code"]
```

```python
def test_inactive_deployments_are_not_routing_candidates(route_config):
    resolved = resolve_model_request("kimi-k2.7-code", route_config, GatewayRoutingState(), now=1000.0, env={})
    assert resolved.kind == "unavailable"
    assert resolved.ordered_deployments == []
```

```python
def test_unknown_explicit_model_does_not_fallback_to_default(route_config):
    resolved = resolve_model_request("typo-model", route_config, GatewayRoutingState(), now=1000.0, env={})
    assert resolved.kind == "not-found"
    assert resolved.ordered_deployments == []
```

- [ ] **Step 3: Write failing fallback classification tests**

Add tests in `src/router/tests/test_routing.py`:

```python
def test_retryable_statuses_fallback_to_next_deployment():
    assert is_retryable_failure(429)
    assert is_retryable_failure(503)
    assert is_retryable_failure("transport_error")
    assert is_retryable_failure("provider quota exceeded")
```

```python
def test_caller_errors_do_not_fallback():
    assert not is_retryable_failure(400)
    assert not is_retryable_failure(401)
    assert not is_retryable_failure(403)
    assert not is_retryable_failure(422)
```

Add an app-level test in `src/router/tests/test_app.py`:

```python
def test_chat_retries_ordered_deployments_only_for_retryable_failures(client, upstream):
    upstream.enqueue_json(status_code=429, body={"error": "rate limited"})
    upstream.enqueue_json(status_code=200, body={"choices": [{"message": {"content": "ok"}}]})

    response = client.post(
        "/v1/chat/completions",
        json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert upstream.requests[0].json()["model"] == "ollama-local.kimi-k2.7-code"
    assert upstream.requests[1].json()["model"] == "opencode-go.kimi-k2.7-code"
```

Add a non-retry test:

```python
def test_chat_does_not_fallback_for_caller_error(client, upstream):
    upstream.enqueue_json(status_code=400, body={"error": "bad request"})

    response = client.post(
        "/v1/chat/completions",
        json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 400
    assert len(upstream.requests) == 1
```

Add virtual-key pass-through and allowlist tests:

```python
def test_virtual_key_is_forwarded_with_rewritten_deployment_model(client, upstream):
    upstream.enqueue_json(status_code=200, body={"choices": [{"message": {"content": "ok"}}]})

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-virtual"},
        json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert upstream.requests[0].headers["authorization"] == "Bearer sk-virtual"
    assert upstream.requests[0].json()["model"] == "ollama-local.kimi-k2.7-code"
```

```python
def test_litellm_virtual_key_403_is_not_fallback(client, upstream):
    upstream.enqueue_json(
        status_code=403,
        body={"error": {"type": "key_model_access_denied", "message": "not allowed"}},
    )

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-virtual"},
        json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 403
    assert len(upstream.requests) == 1
```

Add warm-session tests:

```python
def test_no_x_session_id_does_not_pin_previous_deployment(app, client, upstream):
    upstream.enqueue_json(status_code=200, body={"choices": [{"message": {"content": "ok"}}]})
    upstream.enqueue_json(status_code=200, body={"choices": [{"message": {"content": "ok"}}]})

    client.post("/v1/chat/completions", json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "a"}]})
    app.state.routing_state.record_latency("ollama-local.kimi-k2.7-code", 900)
    app.state.routing_state.record_latency("opencode-go.kimi-k2.7-code", 100)
    client.post("/v1/chat/completions", json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "b"}]})

    assert upstream.requests[0].json()["model"] == "ollama-local.kimi-k2.7-code"
    assert upstream.requests[1].json()["model"] == "opencode-go.kimi-k2.7-code"
```

```python
def test_warm_session_is_scoped_by_auth_fingerprint(app, client, upstream):
    upstream.enqueue_json(status_code=200, body={"choices": [{"message": {"content": "ok"}}]})
    upstream.enqueue_json(status_code=200, body={"choices": [{"message": {"content": "ok"}}]})

    client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer key-a", "X-Session-Id": "same"},
        json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "a"}]},
    )
    app.state.routing_state.record_latency("ollama-local.kimi-k2.7-code", 900)
    app.state.routing_state.record_latency("opencode-go.kimi-k2.7-code", 100)
    client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer key-b", "X-Session-Id": "same"},
        json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "b"}]},
    )

    assert upstream.requests[0].json()["model"] == "ollama-local.kimi-k2.7-code"
    assert upstream.requests[1].json()["model"] == "opencode-go.kimi-k2.7-code"
```

```python
def test_warm_session_reuses_same_deployment_for_same_auth_and_session(app, client, upstream):
    upstream.enqueue_json(status_code=200, body={"choices": [{"message": {"content": "ok"}}]})
    upstream.enqueue_json(status_code=200, body={"choices": [{"message": {"content": "ok"}}]})

    headers = {"Authorization": "Bearer key-a", "X-Session-Id": "same"}
    client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "a"}]},
    )
    app.state.routing_state.record_latency("ollama-local.kimi-k2.7-code", 900)
    app.state.routing_state.record_latency("opencode-go.kimi-k2.7-code", 100)
    client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "b"}]},
    )

    assert upstream.requests[0].json()["model"] == "ollama-local.kimi-k2.7-code"
    assert upstream.requests[1].json()["model"] == "ollama-local.kimi-k2.7-code"
```

Add response metadata tests:

```python
def test_chat_success_sets_gateway_routing_headers(client, upstream):
    upstream.enqueue_json(status_code=200, body={"choices": [{"message": {"content": "ok"}}]})

    response = client.post(
        "/v1/chat/completions",
        json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.headers["X-Gateway-Requested-Model"] == "kimi-k2.7-code"
    assert response.headers["X-Gateway-Model-Kind"] == "registry-model"
    assert response.headers["X-Gateway-Served-Deployment"] == "ollama-local.kimi-k2.7-code"
    assert response.headers["X-Gateway-Fallback-Count"] == "0"
```

```python
def test_no_active_deployments_returns_diagnostic_503(client, monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)

    response = client.post(
        "/v1/chat/completions",
        json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 503
    payload = response.json()
    assert payload["error"]["type"] == "gateway_no_active_deployment"
    assert payload["error"]["model"] == "kimi-k2.7-code"
```

- [ ] **Step 4: Run tests to verify failure**

Run:

```bash
python3 -m pytest src/router/tests/test_routing_state.py src/router/tests/test_routing.py src/router/tests/test_app.py -q
```

Expected: fail because routing state, model resolution, and fallback classification do not exist.

- [ ] **Step 5: Implement in-memory state and scoring**

Use process-local state. No Redis and no DB for v1.

Scoring:

```python
score = (
    weights.health * health_score
    + weights.latency * latency_score
    + weights.quota * quota_score
    + weights.stability * stability_score
    + weights.connection_density * density_score
    + weights.priority * priority_score
)
```

Normalize:

- `health_score`: successes / attempts, unknown is `1.0`
- `latency_score`: unknown is `1.0`; otherwise inverse relative to the slowest candidate
- `quota_score`: `0.0` when the deployment had recent `402`, `429`, or status text containing `quota` or `rate`; otherwise `1.0`
- `stability_score`: deployment config value, default `0.8`
- `density_score`: `1.0 - active/max_concurrent`; unknown max is `1.0`
- `priority_score`: best priority in the candidate set is `1.0`, worst is `0.0`

Tie-breaker: keep configured or generated candidate order stable.

- [ ] **Step 6: Implement fallback classification**

Add one shared predicate used by chat and streaming before attempting the next deployment:

```python
def is_retryable_failure(status: int | str) -> bool:
    if isinstance(status, int):
        return status in {408, 409, 425, 429, 500, 502, 503, 504}
    lowered = status.lower()
    return any(word in lowered for word in ("transport", "timeout", "quota", "rate", "overloaded", "unavailable"))
```

Do not retry `400`, `401`, `403`, `404`, or `422`.

- [ ] **Step 7: Wire request handling**

Request flow:

```text
client model -> resolve kind and ordered deployments -> LiteLLM model rewrite
```

Rules:

- explicit valid model wins, whether combo, registry-model, or connection-model
- resolution uses the same `active_deployment_ids()` filter as `/v1/models`
- missing model falls back to `default_model`
- unknown explicit model returns `gateway_model_not_found`
- if resolution has no active deployments, return `gateway_no_active_deployment`
- warm session can move its last deployment to the front if it still belongs to the resolved candidate set and is not in quota cooldown
- failed retryable attempts continue through ordered deployments
- client-facing response headers/logs include requested model kind and served deployment

- [ ] **Step 8: Track attempts in chat and streaming**

Wrap each upstream attempt:

```python
token = app.state.routing_state.start_attempt(deployment_id)
started = time.perf_counter()
try:
    upstream = await client.post(...)
finally:
    latency_ms = (time.perf_counter() - started) * 1000
    app.state.routing_state.finish_attempt(token, status=status, latency_ms=latency_ms)
```

Use status `transport_error` for timeout/network exceptions.

- [ ] **Step 9: Re-run focused tests**

Run:

```bash
python3 -m pytest src/router/tests/test_routing_state.py src/router/tests/test_routing.py src/router/tests/test_app.py src/router/tests/test_streaming.py src/router/tests/test_dashboard.py -q
```

Expected: pass.

- [ ] **Step 10: Commit**

```bash
git add src/router/routing_state.py src/router/routing.py src/router/main.py src/router/metrics.py src/router/dashboard.py src/router/tests/test_routing_state.py src/router/tests/test_routing.py src/router/tests/test_app.py src/router/tests/test_streaming.py src/router/tests/test_dashboard.py
git commit -m "feat: route live catalog models"
```

---

### Task 5: Local Gateway CLI and Client Setup

**Files:**

- Create: `src/scripts/gateway.py`
- Create: `src/clients/opencode_plugin/index.js`
- Create: `src/router/tests/test_gateway_cli.py`
- Delete: `src/scripts/generate_opencode_config.py`
- Delete or rewrite: `src/router/tests/test_generate_opencode_config.py`
- Modify: `README.md`
- Modify: `src/README.md`

**Interfaces:**

- Produces:
  - `python3 src/scripts/gateway.py generate`
  - `python3 src/scripts/gateway.py doctor`
  - `python3 src/scripts/gateway.py explain coder`
  - `python3 src/scripts/gateway.py models --view all`
  - `python3 src/scripts/gateway.py setup codex --catalog all --dry-run`
  - `python3 src/scripts/gateway.py setup opencode --mode local-plugin --catalog all --apply`
  - `python3 src/scripts/gateway.py setup opencode --mode static --catalog combos --apply --path /tmp/opencode.json`
  - `python3 src/scripts/gateway.py doctor opencode`

- [ ] **Step 1: Write failing CLI tests**

Use `tmp_path` and `capsys`:

```python
def test_models_command_prints_full_catalog(capsys):
    exit_code = main(["models", "--view", "all"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "coder" in out
    assert "kimi-k2.7-code" in out
```

```python
def test_setup_dry_run_does_not_write(tmp_path, capsys):
    target = tmp_path / "opencode.json"
    plugin_dir = tmp_path / "plugins"
    exit_code = main([
        "setup",
        "opencode",
        "--mode",
        "local-plugin",
        "--catalog",
        "all",
        "--path",
        str(target),
        "--plugin-dir",
        str(plugin_dir),
        "--dry-run",
    ])
    assert exit_code == 0
    assert not target.exists()
    assert not plugin_dir.exists()
    assert "localhost:4100/v1" in capsys.readouterr().out
```

```python
def test_opencode_local_plugin_setup_installs_plugin_without_static_models(tmp_path):
    target = tmp_path / "opencode.json"
    plugin_dir = tmp_path / "plugins" / "agent-ai-gateway"
    exit_code = main([
        "setup",
        "opencode",
        "--mode",
        "local-plugin",
        "--catalog",
        "all",
        "--path",
        str(target),
        "--plugin-dir",
        str(plugin_dir),
        "--apply",
    ])
    assert exit_code == 0
    data = json.loads(target.read_text())
    assert ["./plugins/agent-ai-gateway/index.js", {
        "providerId": "gateway",
        "displayName": "Agent AI Gateway",
        "baseURL": "http://localhost:4100/v1",
        "apiKey": "{env:VIRTUAL_KEY}",
        "catalog": "all",
        "modelCacheTtl": 300000,
    }] in data["plugin"]
    assert "gateway" not in data.get("provider", {})
    assert (plugin_dir / "index.js").exists()
```

```python
def test_opencode_static_setup_writes_catalog_snapshot(tmp_path):
    target = tmp_path / "opencode.json"
    exit_code = main([
        "setup",
        "opencode",
        "--mode",
        "static",
        "--catalog",
        "combos",
        "--path",
        str(target),
        "--apply",
    ])
    assert exit_code == 0
    data = json.loads(target.read_text())
    assert data["provider"]["gateway"]["npm"] == "@ai-sdk/openai-compatible"
    assert data["provider"]["gateway"]["options"]["apiKey"] == "{env:VIRTUAL_KEY}"
    assert "coder" in data["provider"]["gateway"]["models"]
```

```python
def test_setup_apply_writes_backup(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text('{"keep": true}\n')
    exit_code = main(["setup", "pi", "--catalog", "combos", "--path", str(target), "--apply"])
    assert exit_code == 0
    assert list(tmp_path.glob("settings.json.bak.*"))
    assert "agent-ai-gateway" in target.read_text()
```

```python
def test_setup_preserves_unrelated_json_keys(tmp_path):
    target = tmp_path / "opencode.json"
    target.write_text('{"theme": "dark", "provider": {"other": {"name": "keep"}}}\n')
    exit_code = main(["setup", "opencode", "--mode", "static", "--catalog", "all", "--path", str(target), "--apply"])
    assert exit_code == 0
    data = json.loads(target.read_text())
    assert data["theme"] == "dark"
    assert data["provider"]["other"]["name"] == "keep"
    assert "gateway" in data["provider"]
```

```python
def test_setup_invalid_json_aborts_without_backup(tmp_path, capsys):
    target = tmp_path / "opencode.json"
    target.write_text("{not json")
    exit_code = main(["setup", "opencode", "--path", str(target), "--apply"])
    assert exit_code == 1
    assert target.read_text() == "{not json"
    assert not list(tmp_path.glob("opencode.json.bak.*"))
    assert "invalid JSON" in capsys.readouterr().err
```

```python
def test_doctor_opencode_reports_plugin_and_config_status(tmp_path, capsys):
    target = tmp_path / "opencode.json"
    plugin_dir = tmp_path / "plugins" / "agent-ai-gateway"
    main([
        "setup",
        "opencode",
        "--mode",
        "local-plugin",
        "--path",
        str(target),
        "--plugin-dir",
        str(plugin_dir),
        "--apply",
    ])
    exit_code = main(["doctor", "opencode", "--path", str(target), "--plugin-dir", str(plugin_dir)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "configured" in out
    assert "local-plugin" in out
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
python3 -m pytest src/router/tests/test_gateway_cli.py -q
```

Expected: fail because `src/scripts/gateway.py` does not exist.

- [ ] **Step 3: Implement `gateway.py` with `argparse`**

Commands:

- `generate`: calls existing `generate_configs.generate()`
- `doctor`: loads catalog, prints provider count, active connection count, combo count, missing env vars, and config paths
- `doctor CLIENT`: checks a specific client install/config status
- `explain MODEL`: prints whether model is combo, registry-model, or connection-model, plus candidate deployments
- `models --view VIEW`: prints the same catalog entries `/v1/models` would expose
- `setup CLIENT`: renders client config, defaults to dry-run, writes only with `--apply`

Do not import FastAPI app code into the CLI.

Shared setup algorithm:

```text
resolve target base URL from --remote/--base-url, config clients.base_url, or localhost default
normalize to exactly one /v1 suffix for OpenAI-compatible clients
load catalog entries from in-process config for local dry-runs
fetch GET <baseURL>/models?view=<catalog> when --remote is used or --live is requested
render per-client config from one client registry
validate existing JSON/TOML before creating backups
dry-run prints target paths and redacted config
apply writes backups, then target files
doctor reads the target files and reports configured/not_configured/not_installed/unknown
```

- [ ] **Step 4: Implement client renderers**

Keep renderers in `gateway.py` for v1. If they exceed roughly 250 lines, split to `src/scripts/client_setup.py` in the same task.

Client registry fields:

```text
id
display_name
default_paths
config_format
setup_modes
default_setup_mode
supports_catalog_snapshot
supports_dynamic_catalog
supports_env_api_key_ref
base_url_suffix
doctor_checks
```

Supported clients:

- `opencode`: local-plugin mode by default; static OpenAI-compatible provider snapshot as fallback
- `pi`: JSON object with `baseUrl`, `apiKey`, `model`, `_managedBy`, and catalog model list when supported
- `codex`: TOML text for a managed gateway provider/profile block; include selected default model and catalog comment block
- `claude-code`: JSON settings/env snippet; include selected default model and base URL

Rules:

- `--catalog` accepts `all`, `combos`, `registry`, `connections`
- `--mode` accepts only modes registered for the target client
- `--dry-run` prints rendered content and target path
- `--apply` creates parent dirs and writes the file
- existing file gets `path.bak.YYYYmmdd_HHMMSS`
- `--api-key` overrides env lookup
- `--api-key-env` overrides the env var reference name
- `--remote` and `--base-url` configure the client against a non-local gateway
- missing API key in dry-run prints `$VIRTUAL_KEY` placeholder
- missing API key in apply exits nonzero with a clear message
- OpenCode uses `{env:VIRTUAL_KEY}` by default, so missing literal API key does not block apply for OpenCode
- setup never writes the LiteLLM master key unless the caller explicitly passes it with `--api-key`

OpenCode local-plugin renderer:

```text
copy src/clients/opencode_plugin/index.js to ~/.config/opencode/plugins/agent-ai-gateway/index.js
merge opencode.json, preserving unrelated keys
append or replace exactly one plugin tuple whose first element is ./plugins/agent-ai-gateway/index.js
set model to gateway/<clients.targets.opencode.model>
set small_model to gateway/<clients.targets.opencode.small_model> when configured
do not write provider.gateway.models
do not delete unrelated providers or plugins
```

OpenCode plugin contract:

```text
options.providerId default: gateway
options.displayName default: Agent AI Gateway
options.baseURL default: http://localhost:4100/v1
options.apiKey default: {env:VIRTUAL_KEY}
options.catalog default: all
options.modelCacheTtl default: 300000 ms
fetch GET <baseURL>/models?view=<catalog>
emit provider id options.providerId
emit provider npm @ai-sdk/openai-compatible
emit provider options.baseURL and options.apiKey
map every returned model id into a provider model entry with name/display metadata from the gateway response
cache successful model fetches in memory for modelCacheTtl
on fetch failure, keep the last successful in-memory catalog for this OpenCode process
```

OpenCode static renderer:

```text
merge provider.gateway with npm @ai-sdk/openai-compatible
set provider.gateway.options.baseURL to normalized /v1 URL
set provider.gateway.options.apiKey to {env:<api-key-env>}
write provider.gateway.models from the selected live catalog view
set model to gateway/<default model>
set small_model to gateway/<small model> when configured
```

Codex renderer:

```text
write a managed gateway provider/profile block
base_url uses /v1
api key references an env var when Codex supports it; otherwise dry-run prints an export command
default profile uses clients.targets.codex.model
optional generated profiles may be created from catalog metadata later, but v1 must not invent hardcoded model categories
```

Claude Code renderer:

```text
write settings/env for ANTHROPIC_BASE_URL without /v1 when using Anthropic-compatible surface
write model to clients.targets.claude-code.model
write auth token as env reference or setup instructions, not a literal key by default
do not generate per-model profiles in v1 unless Claude Code can consume them without extra launcher magic
```

Pi renderer:

```text
write only the documented Pi config shape for base URL, key, default model, and optional models
if Pi has no supported env substitution, require --api-key for --apply
```

- [ ] **Step 5: Remove old opencode script**

Delete `src/scripts/generate_opencode_config.py`. Replace references in docs with:

```bash
python3 src/scripts/gateway.py setup opencode --mode local-plugin --catalog all --dry-run
python3 src/scripts/gateway.py setup opencode --mode local-plugin --catalog all --apply
python3 src/scripts/gateway.py setup opencode --mode static --catalog all --apply
```

- [ ] **Step 6: Re-run CLI tests**

Run:

```bash
python3 -m pytest src/router/tests/test_gateway_cli.py -q
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add src/scripts/gateway.py src/router/tests/test_gateway_cli.py README.md src/README.md
git rm src/scripts/generate_opencode_config.py src/router/tests/test_generate_opencode_config.py
git commit -m "feat: add live catalog gateway CLI"
```

---

### Task 6: Docs, Verification, and Simplification Review

**Files:**

- Modify: `README.md`
- Modify: `src/README.md`
- Modify: `docs/models.md`
- Modify: `docs/PLAN.md` if it still describes alias-first routing
- Modify: generated YAML after running the generator

**Interfaces:**

- Produces:
  - user docs for adding provider registry metadata, connections, combos, and client setup
  - verified generated configs
  - over-engineering review applied to the final diff

- [ ] **Step 1: Update docs with the model kinds**

Document:

```text
Provider: reusable adapter and registry metadata.
Connection: one configured local endpoint/account/key for a provider.
Combo: curated public model with fallback/scoring.
Registry model: provider model id served by active connections.
Connection model: explicit qualified deployment id.
Client: local harness setup target.
```

- [ ] **Step 2: Add cookbook commands**

Include:

```bash
python3 src/scripts/gateway.py doctor
python3 src/scripts/gateway.py generate
python3 src/scripts/gateway.py models --view all
python3 src/scripts/gateway.py models --view combos
python3 src/scripts/gateway.py explain kimi-k2.7-code
python3 src/scripts/gateway.py setup codex --catalog all --dry-run
python3 src/scripts/gateway.py setup opencode --mode local-plugin --catalog all --apply
curl "http://localhost:4100/v1/models?view=all" -H "Authorization: Bearer $VIRTUAL_KEY"
```

Document virtual-key allowlists explicitly:

```text
Because the router rewrites catalog ids to deployment ids before calling LiteLLM,
LiteLLM virtual keys must allow the internal deployment ids they may use, such as
ollama-local.kimi-k2.7-code and deepseek-api.deepseek-v4-pro.
```

- [ ] **Step 3: Remove old alias-first docs**

Remove instructions that tell users to edit `models` and `aliases`. Replace with provider/connection/combo examples.

- [ ] **Step 4: Run generated config check**

Run:

```bash
python3 src/scripts/gateway.py generate
git diff -- src/litellm.config.yaml src/router/router_config.yaml
```

Expected: generated YAML changes are intentional and committed.

- [ ] **Step 5: Run full test/lint/type suite**

Run:

```bash
python3 -m pytest -q
python3 -m ruff check .
python3 -m ruff format --check .
python3 -m mypy
```

Expected: all pass.

- [ ] **Step 6: Run ponytail review**

Use `ponytail-review` on the final diff. Apply cuts unless they conflict with:

- explicit provider/connection/combo/live-catalog architecture
- live model experimentation
- scoring requirements
- tests
- security around secrets and client config writes

- [ ] **Step 7: Optional local smoke test**

If Docker is available and env vars are present:

```bash
docker compose -f src/docker-compose.yml up -d --build
curl http://localhost:4100/healthz
curl "http://localhost:4100/v1/models?view=all" -H "Authorization: Bearer $VIRTUAL_KEY"
```

Expected: health succeeds and `/v1/models` lists combos, registry models, and connection models.

- [ ] **Step 8: Commit docs and verification fixes**

```bash
git add README.md src/README.md docs/models.md docs/PLAN.md src/gateway.config.yaml src/litellm.config.yaml src/router/router_config.yaml
git commit -m "docs: document live model catalog"
```

---

## Self-Review

**Spec coverage**

- Active connections + registry metadata + combos -> live client catalog: Tasks 1, 2, and 3.
- `/v1/models` defaults to full live catalog: Task 3.
- Filtered model views for noisy clients: Task 3 and Task 5.
- Experimentation with direct models: Task 3 and Task 4.
- Harnesses with mode-specific models: Task 5 supports `--catalog all|combos|registry|connections`.
- No migration/backcompat: Global constraints, Task 1 replacement YAML, Task 5 deletion of old opencode script.
- Fallback signals: Task 4 covers health, latency, quota cooldown, stability, active density, and priority.
- Client onboarding: Task 5 covers Codex, Claude Code, OpenCode, and Pi as downstream clients.
- Codex/Claude/Copilot account-backed upstream providers: explicitly out of scope for this pass.
- Docs and verification: Task 6.

**Ponytail cuts applied**

- Provider registry metadata lives in YAML, not a plugin marketplace.
- Quota cooldown instead of a token quota ledger.
- One CLI front door instead of separate setup scripts.
- JSON dashboard extension only, no frontend redesign.
- Delete obsolete opencode generator instead of wrapping it.

**Type consistency**

- `GatewayCatalog`, active deployments, combos, registry models, and connection models are used consistently.
- Routing state consumes deployment ids.
- Clients consume live catalog ids.

**No placeholders**

- No undefined future implementation steps.
- Optional smoke test is explicitly gated on Docker/env availability.

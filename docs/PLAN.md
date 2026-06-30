# Personal Cache-Aware AI Gateway Plan

A lightweight AI gateway for one person and their local agents, runnable as a
Docker stack on a NAS. The goal is the Coinbase pattern scaled down: one
OpenAI-compatible endpoint, cheaper defaults, Redis-backed caching, sticky
conversation routing, provider failover, virtual keys, and enough spend
visibility to avoid surprises.

## Source-Backed Assumptions

- This plan chooses LiteLLM as the runtime gateway and treats Portkey as the
  product benchmark. Both are valid AI gateways, but stacking Portkey in front
  of LiteLLM would duplicate provider routing, retries, fallbacks, caching,
  guardrails, key handling, and logs in a small personal deployment.
- LiteLLM remains the provider adapter and key/spend layer. Its proxy exposes an
  OpenAI-format gateway across many providers, supports model aliases, Redis
  caching, router fallbacks, virtual keys, model access limits, and provider or
  model budgets.
- Portkey's OSS gateway is useful prior art for the shape of this project:
  OpenAI-compatible edge API, config-driven routing, retries/fallbacks,
  guardrails, local console/logs, smart caching, and many provider integrations.
  Borrow those product ideas, not the whole stack, unless LiteLLM blocks a
  required provider or policy.
- Keep Postgres. LiteLLM documents Postgres as the database for virtual keys,
  users, budgets, and per-request usage tracking, and its database Docker image
  is designed for Postgres-backed proxy deployments. SQLite is not the hardened
  path for the features this gateway needs.
- Redis has two jobs: LiteLLM response/cache state and the custom router's
  session-stickiness state.
- The custom router stays thin. LiteLLM already handles provider translation,
  retries, fallback chains, rate limits, keys, and spend logging. The sidecar
  should only add personal policy that LiteLLM does not currently cover well:
  cache-aware model stickiness, simple task classification, stable cache keys,
  and local redaction hooks.
- Prefer Chat Completions compatibility at the outside edge because most coding
  agents still speak `/v1/chat/completions`. The router can pass through
  `/v1/models` and later add `/v1/responses` once the clients need it.

## Build Gate: Current Subscriptions

Do not start implementation unless the current paid services can be connected
in a useful way. "Connected" means either:

- **Upstream provider:** the gateway can call the service through an API or
  supported provider adapter.
- **Gateway client:** the tool can call this gateway, but its subscription is
  not exposed as a reusable model provider.

Current account mapping:

| Account/subscription | Gateway role | Status | Implementation requirement |
|---|---|---|---|
| OpenCode Go subscription | Upstream provider | **Required / feasible** | Call `https://opencode.ai/zen/go/v1/chat/completions` with `OPENCODE_GO_API_KEY`; smoke test `/models` first because the model list changes. |
| Ollama Cloud | Upstream provider | **Required / feasible, verify auth** | Use `OLLAMA_API_KEY` against Ollama's hosted API; verify whether LiteLLM can pass cloud auth cleanly before relying on it in fallback chains. |
| Copilot Pro | Upstream provider | **Skipped for now** | Do not configure this in the first implementation. Revisit LiteLLM's `github_copilot/` OAuth device flow later if the token can persist across NAS restarts. |
| DeepSeek API | Upstream provider | **Required / feasible** | Use `DEEPSEEK_API_KEY` with DeepSeek's OpenAI-compatible API and current V4 model names. |
| Codex Pro | Gateway client, not upstream provider | **Required / feasible as client only** | Codex can define a custom model provider that points at this gateway and uses a LiteLLM virtual key, or it can use ChatGPT sign-in separately. Codex Pro subscription access is not a general upstream API key for other clients. |

Decision rule:

- **Go:** OpenCode Go, Ollama Cloud, and DeepSeek API each pass a one-request
  smoke test through LiteLLM or the sidecar, and Codex Pro can call the gateway
  as a client via a custom model provider or `OPENAI_BASE_URL`.
- **No-go:** If the requirement is that Codex Pro must be usable as a generic
  upstream provider behind the gateway. OpenAI documents Codex ChatGPT sign-in
  as subscription access for Codex clients, while API-key use is billed through
  the OpenAI Platform at standard API rates.
- **Later:** Copilot Pro can be added as a separate spike after the core
  gateway is working.

## Runtime Decision

### Recommended Runtime: LiteLLM + Thin Router

Use LiteLLM for provider normalization, model aliases, virtual keys, spend
tracking, Redis cache integration, fallbacks, and the admin UI. Put a small
FastAPI router in front only for personal policy that is specific to this
project: cache-aware model stickiness, deterministic task classification,
redaction before vendor egress, and a stable single endpoint for agents.

Why this stays lightweight:

- The repo already runs LiteLLM with Docker, Postgres, and Redis.
- LiteLLM's upstream Docker compose already assumes Postgres for proxy state;
  this matches the existing local stack.
- LiteLLM supports the provider-specific oddities this plan needs, including
  OpenAI-compatible custom bases, Ollama, Z.AI, DeepSeek, GitHub Copilot, and
  OpenCode integration notes.
- The custom code remains small and replaceable. If LiteLLM later gains native
  cache-aware sticky routing, the sidecar can shrink or disappear.

### Portkey-Inspired Features To Keep

Portkey is a good reference for what a polished personal gateway should feel
like, especially because its OSS gateway emphasizes a single OpenAI-compatible
edge API, routing configs, retries/fallbacks, guardrails, smart caching, local
logs, and a tiny local Docker/Node footprint.

Copy these ideas into the LiteLLM-based build:

- Config-driven routes and fallback policy instead of hard-coded logic.
- Explicit guardrail/redaction layer before requests leave the NAS.
- Local request console/log summaries, but without prompt body logging by
  default.
- Simple cache controls first; semantic cache only after plain prefix/session
  caching is measured.
- Cost/pricing awareness in docs and dashboards, using upstream model pricing
  metadata where available.

### Alternative Runtime: Portkey Only

Portkey can be revisited if the personal priority shifts toward guardrails,
local console UX, semantic caching, and config-driven policy over LiteLLM's
provider-specific adapters and Postgres-backed virtual-key/spend model.

Do not run Portkey and LiteLLM together in the first implementation. If Portkey
is evaluated, make it a separate spike with the same provider matrix and
verification checklist, then choose one gateway as the single provider adapter.

## Architecture

```text
agents ──OpenAI API──▶ sticky-router (FastAPI, :4100) ──▶ litellm (:4000, internal) ──▶ providers
                              │                              │
                         redis (:6379) ◀─────────────────────┘  cache + sessions
                              │
                        postgres (:5432)  LiteLLM keys, spend, budgets, UI state
```

**4 containers:** `sticky-router`, `litellm`, `postgres`, `redis`.

Only port `4100` is exposed to agents. LiteLLM `4000`, Postgres `5432`, and
Redis `6379` stay on the internal Docker network. The LiteLLM admin UI remains
reachable only through the router bypass path on the NAS or over Tailscale when
explicitly enabled for maintenance.

## Provider Matrix

The first implementation should cover these aliases. Model IDs are deliberately
specific where provider docs now name current models, but they should live in
config so they can be changed without code edits.

| Alias | LiteLLM route | Env | Purpose | Notes |
|---|---|---|---|---|
| `fast` | `deepseek/deepseek-v4-flash` | `DEEPSEEK_API_KEY` | Default low-cost chat/code draft model | DeepSeek documents `deepseek-chat` and `deepseek-reasoner` as deprecated on 2026-07-24, so do not build new aliases on those names. |
| `deepseek-pro` | `deepseek/deepseek-v4-pro` | `DEEPSEEK_API_KEY` | Stronger DeepSeek path for coding/reasoning | Also useful as fallback when OpenCode Go quota is exhausted. |
| `openai-fast` | `openai/<small-model>` | `OPENAI_API_KEY` | Optional future OpenAI API baseline | Only enable if there is a separate OpenAI Platform API key. Codex Pro does not satisfy this env var without standard API billing. |
| `reasoning` | `openai/<reasoning-model>` | `OPENAI_API_KEY` | Optional future OpenAI escalation | Optional unless a separate OpenAI Platform budget is added. Prefer DeepSeek/OpenCode Go first for this subscription-only build. |
| `vision` | `openai/<vision-model>` | `OPENAI_API_KEY` | Optional future image input and screenshots | Optional unless a separate OpenAI Platform budget is added. |
| `zai` | `zai/glm-4.7` | `ZAI_API_KEY` | Optional future GLM reasoning/coding provider | LiteLLM has a first-class `zai/` provider. Prefer that over pretending Z.AI is Ollama. |
| `zai-cheap` | `zai/glm-4.5-flash` | `ZAI_API_KEY` | Optional future GLM fallback | Good candidate for non-critical requests if quota and latency are acceptable. |
| `ollama-local` | `ollama_chat/<local-model>` | `OLLAMA_API_BASE` | Optional future local NAS/LAN Ollama | Use `ollama_chat` for chat; set `api_base` to the local Ollama server. |
| `ollama-cloud` | `ollama_chat/<cloud-model>` | `OLLAMA_API_BASE`, `OLLAMA_API_KEY` | Ollama cloud models | Ollama documents native cloud API at `https://ollama.com/api`; its OpenAI-compatible local/client endpoint is `/v1`. Verify LiteLLM behavior against cloud auth during implementation. |
| `opencodego-fast` | `openai/deepseek-v4-flash` + `https://opencode.ai/zen/go/v1` | `OPENCODE_GO_API_KEY` | Cheap OpenCode Go coding model | OpenCode Go documents OpenAI-compatible `/chat/completions` endpoints for this model. |
| `opencodego-code` | `openai/deepseek-v4-pro` + `https://opencode.ai/zen/go/v1` | `OPENCODE_GO_API_KEY` | Stronger OpenCode Go coding model | Use the OpenAI-compatible subset first; add Anthropic-style Go models later if needed. |
| `copilot` | `github_copilot/<model>` | persisted OAuth token volume | Skipped for first implementation | LiteLLM documents GitHub Copilot via OAuth device flow, not a normal static API key. Bootstrap manually and persist its credentials in a later spike. |
| `github-models` | `github/<model>` | `GITHUB_API_KEY` | Optional future GitHub Models API fallback | This is not Copilot, but it is the official GitHub Models inference API and is easier to automate with a PAT. |

### Provider Notes

- Z.AI has two official endpoint families: the general API at
  `https://api.z.ai/api/paas/v4/` and the Coding Plan API at
  `https://api.z.ai/api/coding/paas/v4`. The first pass should use LiteLLM's
  `zai/` provider. Add a separate OpenAI-compatible alias only if the Coding
  Plan endpoint is needed.
- OpenCode Go exposes a changing model list and a `/v1/models` endpoint. During
  implementation, fetch the model list in a smoke test instead of hard-coding
  every available model.
- Codex Pro is a client entitlement, not a gateway provider. It belongs in the
  runbook as "Codex can use a custom model provider or `OPENAI_BASE_URL` with
  the gateway URL and a LiteLLM virtual key", not in `litellm.config.yaml` as an
  upstream model.
- Copilot is skipped in the first implementation because the initial OAuth
  device flow is interactive. Keep it out of the fallback path until the token
  persistence story is verified.
- OpenCode clients may send `reasoningSummary`; LiteLLM's OpenCode integration
  docs say to drop it for Chat Completions models. Add
  `additional_drop_params: ["reasoningSummary"]` to aliases used by OpenCode.

## Routing Policy

The router exposes `/v1/chat/completions`, `/v1/models`, and `/healthz`.
Requests to other OpenAI-compatible paths return a clear `501` until implemented.

### Session Identity

- Preferred: client sends `X-Session-Id`.
- Fallback: stable hash of the first system message, first user message, and
  caller virtual-key fingerprint.
- Redis key: `session:{id}` with fields `model`, `last_used_ts`,
  `cache_key`, `classification`, and `fallback_count`.

### Cache-Aware Stickiness

OpenAI prompt caching and many provider caches depend on exact prefix matches.
The router should preserve long stable prefixes and avoid mid-session model
switches while a conversation is warm.

1. If `session:{id}` exists and `now - last_used_ts < CACHE_TTL_SECONDS`, keep
   the stored model unless the request explicitly asks for a different model.
2. If the session is new or cold, classify the request and choose the cheapest
   model expected to work.
3. Set `prompt_cache_key` when the upstream accepts it, using a privacy-safe
   hash of the session id and stable prefix. Unknown providers should receive no
   provider-specific cache parameter unless tested.
4. Store the selected model back to Redis with the same TTL.

Default TTL: `600` seconds. Make it configurable.

### Classification Rules

The classifier should be deterministic and boring:

- Image content present -> `vision`.
- Explicit model alias supplied and allowed -> that alias.
- Code-editing words, stack traces, diffs, tool calls, or file paths ->
  `opencodego-fast`, falling back to `fast` if OpenCode Go is unavailable.
- Large design/debug prompts, "analyze", "why", "architecture", "race
  condition", or repeated fallback from cheap models -> `reasoning`.
- Everything else -> `fast`.

Do not call a model just to classify. This is a cost-control router, not a
second hidden agent.

### Fallback Chains

Keep fallback chains in LiteLLM where possible, because LiteLLM already handles
router fallbacks and cooldown behavior. The sidecar should only retry once when
it needs to rewrite the `model` field after a provider-specific failure.

Initial chains:

```yaml
fast: [ollama-cloud]
opencodego-fast: [fast, deepseek-pro]
opencodego-code: [deepseek-pro, fast]
deepseek-pro: [opencodego-code, fast]
ollama-cloud: [fast]
```

## Cost And Access Controls

- Keep LiteLLM virtual keys enabled. Each agent gets a virtual key, not the
  master key.
- Use model allowlists per key for risky tools. For example, a background
  summarizer gets `fast`, `zai-cheap`, and `ollama-local`; coding agents get
  `opencodego-fast`, `opencodego-code`, `fast`, and `reasoning`.
- Add provider/model budgets in LiteLLM config once aliases are stable. Start
  with soft personal limits and alerts rather than hard team bureaucracy.
- Log request metadata needed for cost debugging: chosen alias, fallback alias,
  cache hit/miss when LiteLLM exposes it, caller key alias, latency, and token
  usage. Do not log full prompts by default.

## Security And Privacy

- `.env` stays NAS-only and uncommitted.
- Router validates the incoming bearer token by forwarding to LiteLLM, not by
  implementing a second auth database.
- Redaction hooks run before forwarding: common secret patterns, obvious API
  keys, and `.env`-style lines are replaced with stable placeholders.
- Prompt/body logging is off by default. Debug logging can be enabled per
  request with an admin-only header.
- Only expose `4100` on LAN/Tailscale. No direct public `4000` LiteLLM port.
- Persist Copilot OAuth/cache files in a named Docker volume with restrictive
  permissions. Do not bake them into an image.

## File Layout

```text
src/
  docker-compose.yml
  litellm.config.yaml
  .env.example
  router/
    Dockerfile
    requirements.txt
    main.py
    router_config.yaml
    redaction.py
    classifier.py
    tests/
      test_classifier.py
      test_stickiness.py
      test_redaction.py
  README.md
docs/
  PLAN.md
```

## Phased Implementation

### Phase 1: Harden The Base Gateway

- Run the build-gate smoke tests before writing router code. Fail fast if the
  paid services cannot connect in the roles above.
- Keep Postgres in `docker-compose.yml`; do not replace it with SQLite.
- Make LiteLLM `4000` internal-only.
- Record the runtime choice in the README: LiteLLM is the active gateway;
  Portkey is prior art and an optional future spike, not another container in
  the default stack.
- Add provider aliases for DeepSeek V4, Ollama Cloud, and OpenCode Go.
- Add `additional_drop_params: ["reasoningSummary"]` to aliases used by
  OpenCode.
- Smoke test `/v1/models` and one non-streaming `/v1/chat/completions` call per
  configured provider.

### Phase 2: Add The Sticky Router

- Add the FastAPI sidecar and expose only `4100`.
- Implement `/v1/chat/completions` streaming and non-streaming passthrough.
- Implement Redis-backed session stickiness with configurable TTL.
- Implement deterministic classification and fallback rewrite behavior.
- Pass through `/v1/models` from LiteLLM so clients can discover aliases.

### Phase 3: Cost Controls And Observability

- Create one LiteLLM virtual key per agent/tool.
- Apply model allowlists per key.
- Add provider/model budget config after one week of real usage data.
- Add local metrics or log summaries for daily spend, cache hit rate, fallback
  count, and top aliases.

### Phase 4: NAS Runbook

- Update `src/README.md` for the `4100` agent endpoint and internal `4000`
  admin/maintenance endpoint.
- Document Synology Container Manager deployment.
- Document Copilot bootstrap separately because of the OAuth device flow.
- Add backup instructions for Postgres, Redis, and Copilot credential volumes.

## Verification Checklist

- `docker compose config` validates.
- `docker compose up -d` starts all four containers.
- OpenCode Go `/v1/models` succeeds with `OPENCODE_GO_API_KEY`.
- DeepSeek `/models` or one chat completion succeeds with `DEEPSEEK_API_KEY`.
- Ollama Cloud auth succeeds with `OLLAMA_API_KEY`.
- Copilot is not part of the first implementation smoke test.
- Codex CLI can point at `http://<nas>:4100/v1` with a custom model provider or
  `OPENAI_BASE_URL` and a LiteLLM virtual key, or can be documented as a
  separate ChatGPT-authenticated client.
- `curl http://localhost:4100/healthz` returns healthy router, LiteLLM, Redis,
  and Postgres status.
- `curl http://localhost:4100/v1/models` lists the public aliases.
- Each provider alias succeeds or is marked disabled with a documented reason.
- Two requests with the same `X-Session-Id` stay on the same alias inside TTL.
- A request after TTL can reclassify to a different alias.
- A provider outage follows the configured fallback chain.
- A virtual key with a restricted model allowlist cannot call disallowed aliases.
- Prompt/body logs do not contain raw `.env`-style secrets.

## Documentation Used

- [Portkey AI Gateway repository](https://github.com/Portkey-AI/gateway)
- [Portkey Gateway Docker compose](https://raw.githubusercontent.com/Portkey-AI/gateway/main/docker-compose.yaml)
- [LiteLLM repository](https://github.com/BerriAI/litellm)
- [LiteLLM Docker compose](https://raw.githubusercontent.com/BerriAI/litellm/main/docker-compose.yml)
- [LiteLLM proxy configuration](https://docs.litellm.ai/docs/proxy/configs)
- [LiteLLM Docker quick start](https://docs.litellm.ai/docs/proxy/docker_quick_start)
- [LiteLLM database contents](https://docs.litellm.ai/docs/proxy/db_info)
- [LiteLLM virtual keys](https://docs.litellm.ai/docs/proxy/virtual_keys)
- [LiteLLM caching](https://docs.litellm.ai/docs/proxy/caching)
- [LiteLLM fallbacks and load balancing](https://docs.litellm.ai/docs/proxy/reliability)
- [LiteLLM model access controls](https://docs.litellm.ai/docs/proxy/model_access)
- [LiteLLM OpenAI-compatible endpoints](https://docs.litellm.ai/docs/providers/openai_compatible)
- [LiteLLM OpenCode integration](https://docs.litellm.ai/docs/tutorials/opencode_integration)
- [LiteLLM DeepSeek provider](https://docs.litellm.ai/docs/providers/deepseek)
- [LiteLLM Z.AI provider](https://docs.litellm.ai/docs/providers/zai)
- [LiteLLM Ollama provider](https://docs.litellm.ai/docs/providers/ollama)
- [LiteLLM GitHub Copilot provider](https://docs.litellm.ai/docs/providers/github_copilot)
- [OpenAI API overview](https://developers.openai.com/api/reference/overview)
- [OpenAI models](https://developers.openai.com/api/docs/models)
- [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)
- [Codex authentication](https://developers.openai.com/codex/auth)
- [Codex advanced configuration](https://developers.openai.com/codex/config-advanced)
- [LiteLLM OpenAI Codex integration](https://docs.litellm.ai/docs/tutorials/openai_codex)
- [DeepSeek API quick start](https://api-docs.deepseek.com/)
- [Z.AI OpenAI SDK compatibility](https://docs.z.ai/guides/develop/openai/python)
- [Z.AI HTTP API endpoints](https://docs.z.ai/guides/develop/http/introduction)
- [Ollama API introduction](https://docs.ollama.com/api/introduction)
- [Ollama OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility)
- [Ollama authentication](https://docs.ollama.com/api/authentication)
- [OpenCode Go](https://opencode.ai/docs/go/)
- [GitHub Models inference REST API](https://docs.github.com/en/rest/models/inference)

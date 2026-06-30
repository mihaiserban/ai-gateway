# Personal Cache-Aware AI Gateway Plan

A lightweight AI gateway for one person and their local agents, runnable as a
Docker stack on a NAS. The goal is the Coinbase pattern scaled down: one
OpenAI-compatible endpoint, cheaper defaults, Redis-backed caching, sticky
conversation routing, provider failover, virtual keys, and enough spend
visibility to avoid surprises.

## Source-Backed Assumptions

- LiteLLM remains the provider adapter and key/spend layer. Its proxy exposes an
  OpenAI-format gateway across many providers, supports model aliases, Redis
  caching, router fallbacks, virtual keys, model access limits, and provider or
  model budgets.
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
| `openai-fast` | `openai/gpt-5.4-mini` or `openai/gpt-5.4-nano` | `OPENAI_API_KEY` | Cheap OpenAI-compatible baseline | OpenAI docs recommend smaller variants for latency/cost-sensitive work. |
| `reasoning` | `openai/gpt-5.5` | `OPENAI_API_KEY` | Highest-quality reasoning/coding escalation | Use for hard design/debug tasks, not as the default. |
| `vision` | `openai/gpt-5.5` | `OPENAI_API_KEY` | Image input and screenshots | Current OpenAI docs say latest models support text and image input. |
| `zai` | `zai/glm-4.7` | `ZAI_API_KEY` | GLM reasoning/coding provider | LiteLLM has a first-class `zai/` provider. Prefer that over pretending Z.AI is Ollama. |
| `zai-cheap` | `zai/glm-4.5-flash` | `ZAI_API_KEY` | Free/cheap GLM fallback | Good candidate for non-critical requests if quota and latency are acceptable. |
| `ollama-local` | `ollama_chat/<local-model>` | `OLLAMA_API_BASE` | Local NAS/LAN Ollama | Use `ollama_chat` for chat; set `api_base` to the local Ollama server. |
| `ollama-cloud` | `ollama_chat/<cloud-model>` | `OLLAMA_API_BASE`, `OLLAMA_API_KEY` | Ollama cloud models | Ollama documents native cloud API at `https://ollama.com/api`; its OpenAI-compatible local/client endpoint is `/v1`. Verify LiteLLM behavior against cloud auth during implementation. |
| `opencodego-fast` | `openai/deepseek-v4-flash` + `https://opencode.ai/zen/go/v1` | `OPENCODE_GO_API_KEY` | Cheap OpenCode Go coding model | OpenCode Go documents OpenAI-compatible `/chat/completions` endpoints for this model. |
| `opencodego-code` | `openai/deepseek-v4-pro` + `https://opencode.ai/zen/go/v1` | `OPENCODE_GO_API_KEY` | Stronger OpenCode Go coding model | Use the OpenAI-compatible subset first; add Anthropic-style Go models later if needed. |
| `copilot` | `github_copilot/<model>` | persisted OAuth token volume | Experimental Copilot route | LiteLLM documents GitHub Copilot via OAuth device flow, not a normal static API key. Bootstrap manually and persist its credentials. |
| `github-models` | `github/<model>` | `GITHUB_API_KEY` | Optional GitHub Models API fallback | This is not Copilot, but it is the official GitHub Models inference API and is easier to automate with a PAT. |

### Provider Notes

- Z.AI has two official endpoint families: the general API at
  `https://api.z.ai/api/paas/v4/` and the Coding Plan API at
  `https://api.z.ai/api/coding/paas/v4`. The first pass should use LiteLLM's
  `zai/` provider. Add a separate OpenAI-compatible alias only if the Coding
  Plan endpoint is needed.
- OpenCode Go exposes a changing model list and a `/v1/models` endpoint. During
  implementation, fetch the model list in a smoke test instead of hard-coding
  every available model.
- Copilot is the least NAS-friendly provider because the initial OAuth device
  flow is interactive. Keep it as an optional provider and do not put it in the
  default fallback path until the token persistence story is verified.
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
fast: [zai-cheap, openai-fast, ollama-local]
opencodego-fast: [fast, deepseek-pro, openai-fast]
opencodego-code: [deepseek-pro, reasoning]
reasoning: [deepseek-pro, opencodego-code, openai-fast]
vision: [reasoning]
zai: [deepseek-pro, openai-fast]
copilot: [opencodego-code, deepseek-pro]
ollama-cloud: [ollama-local, fast]
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

- Keep Postgres in `docker-compose.yml`; do not replace it with SQLite.
- Make LiteLLM `4000` internal-only.
- Add provider aliases for OpenAI, DeepSeek V4, Z.AI, Ollama, OpenCode Go,
  Copilot, and optional GitHub Models.
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
- [DeepSeek API quick start](https://api-docs.deepseek.com/)
- [Z.AI OpenAI SDK compatibility](https://docs.z.ai/guides/develop/openai/python)
- [Z.AI HTTP API endpoints](https://docs.z.ai/guides/develop/http/introduction)
- [Ollama API introduction](https://docs.ollama.com/api/introduction)
- [Ollama OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility)
- [Ollama authentication](https://docs.ollama.com/api/authentication)
- [OpenCode Go](https://opencode.ai/docs/go/)
- [GitHub Models inference REST API](https://docs.github.com/en/rest/models/inference)

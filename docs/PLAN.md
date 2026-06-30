# Personal Cache-Aware AI Gateway

A lightweight, personal AI gateway running as a Docker stack on a local NAS.
One OpenAI-compatible endpoint, multiple providers, Redis caching, cheaper
defaults, cross-provider failover, and a custom cache-aware sticky router
(the Coinbase idea, scaled down to one user + their agents).

## Architecture

```
agents ──OpenAI API──▶ sticky-router (FastAPI, :4100) ──▶ litellm (:4000, internal) ──▶ providers
                              │                              │
                         redis (:6379) ◀─────────────────────┘  (cache + session state)
                              │
                         sqlite volume  (litellm DB — keys/spend, no Postgres container)
```

**3 containers:** `sticky-router`, `litellm`, `redis`. SQLite on a volume
replaces Postgres. Only port `4100` is exposed to agents; LiteLLM `4000` is
internal-only.

## Providers (litellm.config.yaml)

| Alias | Provider route | Notes |
|-------|---------------|-------|
| `fast` | `deepseek/deepseek-chat` | Cheapest default |
| `code` | `openai/<model>` + `OPENCODEGO_API_BASE` | opencode.dev Go, OpenAI-compatible, placeholder api_base |
| `reasoning` | `openai/gpt-5.5` | OpenAI |
| `vision` | `openai/gpt-5.5` | OpenAI, vision-capable |
| `zai` | `ollama_chat/glm-4.6` + Ollama Cloud `api_base` | Z.ai/GLM via Ollama Cloud |
| `local` | `ollama_chat/qwen2.5-coder:7b` + Ollama Cloud `api_base` | Via Ollama Cloud |

**Dropped:** Anthropic, OpenRouter, Gemini, Copilot, Postgres.

### Cheaper defaults

`fast` is the catch-all. The router only escalates to `code`/`reasoning` when
it detects the task needs it and the cache is cold. Each request pays full
rate only on new tokens; the prefix reads from Redis cache.

## Cache-aware sticky router

LiteLLM has built-in Redis caching and fallbacks, but not cache-aware model
stickiness. We add a thin FastAPI sidecar on port `4100` — the endpoint agents
talk to.

### Conversation tracking

- Optional `X-Session-Id` header from the agent. Falls back to a hash of
  `system + first user message`.
- Stored in Redis: `session:{id} -> {model, last_used_ts}`.

### Routing decision per request

1. Look up session in Redis.
2. If session exists and `now - last_used_ts < CACHE_TTL` (default 10 min) ->
   keep the same model (cache is warm; switching would invalidate the
   per-model cache prefix).
3. If session is cold (TTL lapsed) or new -> re-evaluate: pick model from
   task signal:
   - Code-shaped request (code blocks / tool calls / "implement"/"refactor")
     -> `code`
   - Long reasoning / "why"/"design"/"analyze" -> `reasoning`
   - Image input -> `vision`
   - Otherwise -> `fast` (cheapest default)
4. Write/update session: `session:{id} = {model, now}` with TTL = `CACHE_TTL`.
5. Forward to LiteLLM at `:4000` with the chosen `model` field, streaming
   passthrough.

### Why this matches the Coinbase idea

The cache is per-model, so switching mid-conversation invalidates it. The
router keeps the model while the cache is warm and only re-routes when the
conversation goes quiet long enough for the TTL to lapse. Each request pays
full rate only on new tokens; the prefix reads from Redis cache.

### Failover

If the chosen model errors, the router falls back to the next in a per-alias
chain (e.g. `reasoning -> code -> fast`). Defined in `router_config.yaml`.

## File layout

```
src/
  docker-compose.yml          # router + litellm + redis + sqlite volume
  litellm.config.yaml          # 6 aliases
  router/
    main.py                    # FastAPI sticky router
    router_config.yaml         # TTL, fallback chains, classification rules
    requirements.txt
  .env.example
  README.md
docs/
  PLAN.md                      # this document
```

## Phased implementation

### Phase 1 — Base gateway

- Rewrite `litellm.config.yaml` with the 6 provider aliases above.
- Switch `docker-compose.yml`: drop `postgres`, add SQLite volume, keep
  `redis`, add `sticky-router` (initially pass-through, no routing logic —
  just forward to LiteLLM).
- Update `.env.example` with new provider keys and remove dropped ones.
- Smoke test each provider alias through LiteLLM directly (`:4000`).

### Phase 2 — Router logic

- Implement the FastAPI router: session tracking, TTL stickiness, task-signal
  classification, failover.
- `router_config.yaml`: alias -> fallback chain, `CACHE_TTL`, classification
  rules.
- Move agent endpoint from `:4000` to `:4100` (router).
- Verify cache hits land (check LiteLLM logs for `cache_hit`), verify
  stickiness across turns, verify re-route after TTL.

### Phase 3 — Hardening / docs

- Network exposure guidance (Tailscale/LAN only, same as today).
- Update `src/README.md` runbook for the new 2-endpoint layout.
- Spend tracking stays on (LiteLLM + SQLite) for personal visibility, but no
  budgets/teams.

## .env.example changes

- Add: `OPENCODEGO_API_KEY`, `OPENCODEGO_API_BASE`, `OLLAMA_CLOUD_API_BASE`,
  `OLLAMA_CLOUD_API_KEY`
- Remove: `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`,
  `POSTGRES_*`, `DATABASE_URL` (-> SQLite path)
- Keep: `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`, `REDIS_PASSWORD`,
  `REDIS_URL`, `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`

## Security

- `.env` gitignored (already). Only `.env.example` with placeholders tracked.
- Master key + salt key stay in `.env` on the NAS.
- Router and LiteLLM on an internal Docker network; only router port (`4100`)
  exposed. LiteLLM `4000` internal-only.
- No public exposure; Tailscale/LAN as today.
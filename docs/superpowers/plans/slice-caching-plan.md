# Slice: P2 Caching — prompt_cache_key passthrough

## Goal

Add a stable `prompt_cache_key` to upstream requests for providers that support it, controlled by config so unknown providers receive no cache key. Cache-hit verification via logs/API is operational and out of scope here.

## Global Constraints

- Keep the existing FastAPI router structure and file layout.
- Do not change docker-compose.
- All new behavior must be covered by tests in `src/router/tests/`. Existing tests must keep passing.
- Test command: `PYTHONPATH=. python3 -m pytest router/tests -q`
- Follow TDD.
- Do not add a new dependency.
- Only set `prompt_cache_key` when the selected alias is in a configurable allowlist; never send it to unconfigured providers.
- The cache key must be privacy-safe: derived from the session id hash, not the raw prompt.

## Requirements

1. **Configurable cache-key allowlist**
   - Add `cache_key_aliases: list[str]` to `RouteConfig` (default empty list).
   - Add `cache_key_aliases:` to `router_config.yaml` (default empty, with a comment listing which providers support it: OpenAI, DeepSeek, Anthropic — not Ollama).
   - Load it in `config.py::_route_config_from_dict`.
2. **Set `prompt_cache_key` in the upstream body**
   - In `chat_completions`, after choosing `current_model`, if `current_model` is in `config.cache_key_aliases`, set `upstream_body["prompt_cache_key"] = <stable value>` derived from the session id hash (e.g. first 32 chars of `sha256(session_id)`).
   - Do not set it for aliases not in the allowlist.
   - The key is set per-attempt (same value across fallback attempts, since session id is stable).
3. **Tests**
   - `test_cache_key_set_for_allowed_alias`: alias in allowlist → upstream body contains `prompt_cache_key` equal to `sha256(session_id)[:32]`.
   - `test_cache_key_not_set_for_disallowed_alias`: alias not in allowlist → upstream body has no `prompt_cache_key`.
   - `test_cache_key_stable_across_fallback`: fallback from allowed to allowed alias keeps the same key.
   - `test_cache_key_absent_by_default`: default config (empty allowlist) → no key sent.

## Notes

- The session id is already a stable SHA-256 (or a client-provided `X-Session-Id`). Hash it again for the cache key to keep it decoupled from the session id itself.
- Cache-hit verification (`X-Cache-Hit` headers, LiteLLM cache metadata) requires a live Redis + LiteLLM instance and is operational; document it as a follow-up, do not implement it here.
- Keep semantic caching out (already enforced by not configuring it in LiteLLM).

## Out of scope

- Live cache-hit verification (operational)
- Semantic caching (explicitly deferred)
- Cost/spend runbook (separate)

## Files likely changed

- `src/router/main.py`
- `src/router/routing.py`
- `src/router/config.py`
- `src/router/router_config.yaml`
- `src/router/tests/test_app.py` or new `src/router/tests/test_cache.py`
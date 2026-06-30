# Slice: P1 Reliability V1 — timeouts and backoff

## Goal

Wire the per-alias `timeouts` config into the httpx client calls and add exponential backoff between router-level fallback retries.

## Global Constraints

- Keep the existing FastAPI router structure and file layout.
- Do not change LiteLLM config or docker-compose.
- All new behavior must be covered by tests in `src/router/tests/`. Existing tests must keep passing.
- Test command: `PYTHONPATH=. python3 -m pytest router/tests -q`
- Follow TDD.
- The `timeouts` dict already exists on `RouteConfig` and in `router_config.yaml` (currently all 120). Wire it; do not redesign the config shape.
- Backoff applies only between router-level fallback attempts (not within LiteLLM's own retry layer).
- Keep backoff bounded and small (personal gateway): base 0.2s, factor 2, max 2.0s, and only sleep between attempts (not before the first).

## Requirements

1. **Per-alias request timeouts**
   - In `src/router/main.py`, replace the hard-coded `timeout=120` in `_proxy`, `_proxy_json`, and `_proxy_stream` with `app.state.route_config.timeouts.get(current_model, 120)`.
   - For `/v1/models` proxy, keep the default 120 (no alias applies).
   - Add a test proving a request to an alias with `timeouts: {alias: 5}` uses a 5-second timeout. Use a mock transport that inspects the client timeout is not feasible via MockTransport directly; instead, assert the chosen timeout value via a small seam: expose a helper `_timeout_for(config, model)` and test it, and have the proxy functions call it.
2. **Exponential backoff between fallback attempts**
   - Between fallback attempts in `chat_completions`, sleep `min(base * (2 ** attempt), max_backoff)` seconds. Use `base=0.2`, `max_backoff=2.0`.
   - Make the backoff parameters configurable on `RouteConfig`: add `retry_base_delay: float = 0.2` and `retry_max_delay: float = 2.0` fields. Add corresponding keys to `router_config.yaml` (under a `retries:` block or top-level; pick top-level for simplicity).
   - Do not sleep before the first attempt or after the final attempt.
   - Add a test proving the router sleeps between attempts. Use a mock clock/async sleep seam: inject an `async_sleep` callable on `app.state` (default `asyncio.sleep`) so tests can record calls without real sleeping. Or patch `asyncio.sleep` via monkeypatch.
   - Keep backoff off the non-retryable path (no sleep after a 400/401/403/404).
3. **Config exposure**
   - Add `retry_base_delay` and `retry_max_delay` to `router_config.yaml` with the defaults above.
   - Load them in `config.py::_route_config_from_dict`.
4. **Tests**
   - `test_per_alias_timeout_used_for_proxy`: assert `_timeout_for(config, "deepseek-pro") == 5` when configured.
   - `test_backoff_sleeps_between_fallback_attempts`: simulate 503 then 200; assert `asyncio.sleep` was called once with the expected delay (0.2s) before the retry.
   - `test_backoff_not_called_on_client_error`: simulate 400; assert no sleep.
   - `test_backoff_grows_exponentially`: simulate three failures; assert sleep called with 0.2, then 0.4 (capped at 2.0). Note: the loop walks the chain; ensure the test uses a chain long enough.

## Notes

- Use `asyncio.sleep` for the actual sleep. Inject a seam for testing.
- Keep changes minimal; do not restructure the retry loop beyond adding the sleep call.
- The `_timeout_for` helper should live in `main.py` or `routing.py`; prefer `routing.py` since `RouteConfig` lives there.

## Out of scope

- Cost/pricing metadata (P2)
- NAS runbook (P2)
- Caching headers (P2)

## Files likely changed

- `src/router/main.py`
- `src/router/routing.py`
- `src/router/config.py`
- `src/router/router_config.yaml`
- `src/router/tests/test_app.py` or new `src/router/tests/test_reliability.py`

# Slice 3: Health and readiness depth

## Goal

Expand `/healthz` to report router, LiteLLM, Redis, and Postgres status, and add a deeper `/readyz` endpoint for dependency readiness.

## Global Constraints

- Keep the existing FastAPI router structure and file layout.
- Do not change LiteLLM config or docker-compose.
- All new behavior must be covered by tests in `src/router/tests/`. Existing tests must keep passing.
- Test command: `PYTHONPATH=. python3 -m pytest router/tests -q`
- Follow TDD.
- Health checks must not crash the router if a dependency is unreachable; report degraded status instead.
- `/healthz` is a liveness check: it should return 200 as long as the router process is up, and include a `status` field plus per-dependency status.
- `/readyz` is a readiness check: it should return 200 only when all dependencies are reachable, otherwise 503, with per-dependency detail.

## Requirements

1. **Expand `/healthz`** to return a JSON body like:
   ```json
   {"status": "ok", "router": "ok", "litellm": "ok|degraded", "redis": "ok|degraded", "postgres": "ok|degraded"}
   ```
   - `status` is `"ok"` if the router is up; `"degraded"` if any dependency is unreachable.
   - Always return HTTP 200 from `/healthz` (liveness).
   - Check LiteLLM by GETing its `/health/liveliness` endpoint (or `/health` if liveliness is unavailable) with a short timeout.
   - Check Redis by PINGing it (if a Redis URL is configured); if no Redis URL is configured, report `"disabled"`.
   - Check Postgres by connecting to `DATABASE_URL` (if configured) and running `SELECT 1`; if no DATABASE_URL is configured, report `"disabled"`.
   - Use short timeouts (e.g., 2 seconds) so a slow dependency does not block the health check.
2. **Add `/readyz`** that returns:
   - HTTP 200 with `{"status": "ready", ...}` when all configured dependencies are reachable.
   - HTTP 503 with `{"status": "not ready", ...}` when any configured dependency is unreachable.
   - Same per-dependency detail as `/healthz`.
3. **Dependency checks**:
   - LiteLLM: reuse the router's `litellm_base_url` and an httpx GET.
   - Redis: use the configured Redis URL via `redis.asyncio` PING. Cache the Redis client on `app.state` to avoid reconnecting on every check.
   - Postgres: use the configured `DATABASE_URL`. Since the router does not currently import a Postgres driver, use a lightweight TCP connect check against the Postgres host:port parsed from `DATABASE_URL` (no SQL driver dependency). If parsing fails or no URL, report `"disabled"`.
4. **Outage simulation test** with mock transport: prove that when LiteLLM returns 503, `/healthz` reports `litellm: degraded` but still returns 200, and `/readyz` returns 503.

## Tests to add

In `src/router/tests/test_health.py` (new):
- `test_healthz_reports_router_ok_when_no_deps_configured`: no Redis/Postgres/LiteLLM reachable, returns 200 with `router: ok` and deps `disabled` or `degraded`.
- `test_healthz_reports_litellm_status`: mock transport returns 200 from LiteLLM liveliness; assert `litellm: ok`.
- `test_healthz_reports_degraded_when_litellm_down`: mock transport raises or returns 503; assert `litellm: degraded`, status `degraded`, HTTP 200.
- `test_readyz_returns_200_when_deps_ok`: LiteLLM ok, Redis disabled/ok, Postgres disabled; returns 200.
- `test_readyz_returns_503_when_litellm_down`: returns 503.
- `test_outage_simulation`: combined scenario showing degraded health and not-ready readyz.

## Notes

- Parse `DATABASE_URL` with `urllib.parse.urlparse` to extract host and port for a TCP connect check. Example: `postgresql://user:pass@host:5432/db`.
- For Redis PING, use `redis.asyncio.from_url(redis_url)` and `await client.ping()`. Cache the client on `app.state.redis_client` lazily.
- Keep health check logic in a helper module or in `main.py`; prefer a small `src/router/health.py` if it keeps `main.py` focused.
- Do not add new heavy dependencies; use stdlib for TCP checks (`socket`).

## Out of scope

- Virtual keys (Slice 4)
- Structured logging (Slice 5)

## Files likely changed

- `src/router/main.py` (wire health endpoints)
- `src/router/health.py` (new, optional)
- `src/router/tests/test_health.py` (new)
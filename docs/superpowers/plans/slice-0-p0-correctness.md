# Slice 0: P0 Correctness & Safety

## Goal
Fix the foundational request-path bugs in the sticky router so sessions are safe, fallbacks actually run, session IDs are stable, and `vision` is not advertised without a working alias.

## Global Constraints
- Keep the existing FastAPI router structure and file layout.
- All routing/session/fallback logic must be unit-testable without Docker or live providers (use `httpx.MockTransport` and `MemorySessionStore`).
- Do not log or store raw bearer tokens. Caller identity may only be used as a one-way hash/fingerprint.
- Streaming passthrough is intentionally out of scope for this slice; it will be handled in Slice 2.
- Config externalization is intentionally out of scope for this slice; keep `RouteConfig` but make it easy to load from config later.
- Follow TDD: write failing tests before production code.

## Requirements

1. **Session writes happen only after upstream success**
   - In `src/router/main.py`, do not call `session_store.set(...)` before the LiteLLM request.
   - Only persist the selected model if the upstream returns a 2xx response.
   - If the upstream fails and the request is retried onto a fallback, do not poison the session with the failed model.
   - After a successful fallback, store the *fallback* model as the session model.

2. **Router-level fallback retry for selected error classes**
   - Use the existing `next_fallback()` helper in `src/router/routing.py` from `main.py`.
   - Retry once on: HTTP `429`, `500`, `502`, `503`, `504`, `httpx.TimeoutException`, `httpx.ConnectError`, `httpx.NetworkError`, `httpx.RemoteProtocolError`.
   - Do **not** retry on: `400`, `401`, `403`, `404` (except treat a `404` from a missing alias as non-retryable).
   - On retry, rewrite `upstream_body["model"]` to the next fallback alias and re-send the request.
   - Increment `fallback_count` stored in the session after a successful fallback.

3. **Response headers reflect routing and fallback**
   - `X-Gateway-Model`: the model that finally produced the response.
   - `X-Gateway-Reason`: the reason for the *final* model (`explicit-model`, `warm-session`, or `classified`).
   - `X-Gateway-Fallback-From`: if a fallback occurred, the originally chosen model.
   - `X-Gateway-Fallback-Count`: number of fallback hops used.

4. **Stable, privacy-safe session IDs**
   - Replace `hash()` in `_fallback_session_id()` with SHA-256.
   - Include a caller-key fingerprint derived from the bearer token (e.g. `sha256(token.encode()).hexdigest()[:16]`) in the session ID input.
   - The session ID input should mix: caller fingerprint + first system message content + first user message content.
   - If no messages or no token, fall back to a deterministic default like `"anonymous"`.

5. **Disable `vision` until an alias exists**
   - Remove `"vision"` from `DEFAULT_ALLOWED_MODELS` in `src/router/routing.py`.
   - Keep the image-content classifier returning `"vision"`, but `choose_model()` should map it to `"fast"` when `vision` is not in `allowed_models`.

6. **Tests**
   - Add `test_failed_upstream_does_not_poison_session`.
   - Add `test_fallback_retry_uses_next_alias`.
   - Add `test_fallback_headers_set_on_retry`.
   - Add `test_stable_session_id_same_token_and_messages`.
   - Add `test_vision_unavailable_routes_to_fast`.
   - Keep all existing tests passing.

## Acceptance Criteria
- `PYTHONPATH=. python3 -m pytest router/tests -q` passes with no warnings.
- New tests cover session safety, fallback retry, fallback headers, stable IDs, and vision guard.
- No raw tokens are logged or stored in session keys/values.

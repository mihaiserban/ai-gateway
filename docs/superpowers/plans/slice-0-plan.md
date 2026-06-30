# Plan: AI Gateway P0 Correctness & Safety

## Goal

Fix the foundational request-path correctness and safety issues in the sticky router so later slices (config, streaming, health, auth) build on a reliable base.

## Global Constraints

- Keep the existing FastAPI router structure in `src/router/`.
- Do not change the LiteLLM `fallbacks` block in `src/litellm.config.yaml`; the router adds its own retry layer on top.
- Session state must remain privacy-safe: never store raw bearer tokens; hash them for identity.
- Tests run with: `PYTHONPATH=. python3 -m pytest router/tests -q`
- Follow existing code style and file organization.

## Task 1: Safe session writes + router-level fallback retry

### Requirements

1. In `src/router/main.py`, do **not** write the session to the store before the upstream LiteLLM call.
2. Only write the session after the upstream call succeeds (HTTP 2xx).
3. If the upstream call fails with a retryable error, use `router.routing.next_fallback` to choose the next model and retry **once**.
4. Retryable errors: HTTP `429`, `500`, `502`, `503`, `504`, `httpx.TimeoutException`, `httpx.NetworkError`, and `httpx.ConnectError`.
5. Non-retryable errors: HTTP `400`, `401`, `403`, `404` (do not retry on 404 unless explicitly required later).
6. On a successful retry, write the session with the final model used and `fallback_count` incremented for each retry step.
7. On a non-retryable failure or exhausted retries, return the upstream response as-is and do **not** write the session.
8. Add/extend tests in `src/router/tests/test_app.py` proving:
   - a failed first model retries the configured fallback;
   - a failed upstream request does not poison sticky session state (second request with same `X-Session-Id` reclassifies/routes as if session were absent);
   - non-retryable errors are returned without retry.

## Task 2: Stable session IDs with caller-key fingerprint

### Requirements

1. Replace `main.py::_fallback_session_id` so it no longer uses Python `hash()`.
2. Build a stable SHA-256 identifier from:
   - first system message content (if present),
   - first user message content (if present),
   - a fingerprint of the caller's bearer token.
3. The token fingerprint must be a hash (e.g., SHA-256 truncated to 16 chars), not the raw token.
4. Keep deterministic output: same inputs must produce the same session ID across processes.
5. If no messages are present, return `"anonymous"`.
6. Add tests in `src/router/tests/test_app.py` or a new `test_session_id.py` proving stability and that changing the caller key changes the session ID.

## Task 3: Disable `vision` alias until configured

### Requirements

1. Remove `"vision"` from `routing.py::DEFAULT_ALLOWED_MODELS`.
2. Remove the `vision` fallback entry from `routing.py::DEFAULT_FALLBACKS`.
3. Keep the classifier returning `"vision"` for image content, but ensure `choose_model` maps it to `"fast"` when `"vision"` is not in `allowed_models` (current behavior already does this — verify and add test if missing).
4. If `litellm.config.yaml` later adds a vision alias, config will re-enable it in a later slice.

## Task 4: Update TODO / PLAN markers (optional, minimal)

### Requirements

1. After implementation, mark the relevant P0 items in `docs/TODO.md` as completed (`[x]`).
2. Do not rewrite the whole TODO; keep changes minimal.

## Verification

- `PYTHONPATH=. python3 -m pytest router/tests -q` passes.
- No new lint/runtime warnings.

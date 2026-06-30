# Personal AI Gateway — P0 Correctness & Safety Implementation Plan

## Goal

Fix the foundational correctness and safety gaps in the sticky router so the gateway can reliably fail over without poisoning session state or leaking unstable session identifiers.

## Global Constraints

- Router stays thin: LiteLLM remains the provider adapter, key/spend layer, and first-line fallback/cooldown mechanism. The router only retries when it must rewrite the `model` field after a provider-specific failure.
- No changes to `litellm.config.yaml` or `docker-compose.yml` in this slice unless absolutely required for correctness.
- All new behavior must be covered by tests in `src/router/tests/`. Existing tests must keep passing.
- Session IDs must not store or transmit raw bearer tokens.
- Streaming is out of scope for this slice.

## Task 1: Safe session writes and router-level fallback retry

### Requirements

- In `src/router/main.py`, do not write the session to the store until the upstream LiteLLM call returns a successful response (2xx). If the upstream call fails with a retryable error, the router may retry against the next fallback alias and only persist the session after success.
- Implement router-level retry once for selected error classes: `429`, `500`, `502`, `503`, `504`, timeout errors, and network errors.
- Do not retry auth/client errors: `400`, `401`, `403`, and most `404`.
- Use `next_fallback()` from `src/router/routing.py` to pick the next alias. Track `fallback_count` accurately in the persisted session.
- On success, set response headers:
  - `X-Gateway-Model`: final selected alias
  - `X-Gateway-Reason`: original decision reason (`explicit-model`, `warm-session`, or `classified`)
  - `X-Gateway-Fallback-From`: original alias when a fallback occurred (optional header, only when different from final model)
  - `X-Gateway-Fallback-Count`: number of fallback attempts used
- If all fallback candidates are exhausted, return the last upstream error response to the client.

### Tests to add/modify

- Add `test_chat_does_not_write_session_on_failed_upstream` in `tests/test_app.py`: simulate a 503 from LiteLLM and assert the next request with the same `X-Session-Id` reclassifies instead of sticking to the failed model.
- Add `test_chat_retries_next_fallback_on_retryable_error` in `tests/test_app.py`: simulate first model 503, second model 200, and assert both models were attempted and final response is 200 with the fallback headers.
- Add `test_chat_does_not_retry_on_client_error` in `tests/test_app.py`: simulate 400 and assert only one upstream call is made.
- Update existing warm-session tests if needed to reflect new header behavior.

## Task 2: Stable fallback session IDs

### Requirements

- Replace the unstable `hash()` based fallback session ID in `src/router/main.py` with a stable SHA-256 derived identifier.
- The identifier must incorporate:
  - The first system message content (if present)
  - The first user message content (if present)
  - A fingerprint of the caller's bearer token (SHA-256 of the token string, not the token itself)
- If no messages are present, return `"anonymous"`.
- Add unit tests in `tests/test_app.py` or a new `tests/test_sessions.py` proving:
  - Same messages + same key produce the same session ID.
  - Same messages + different key produce different session IDs.
  - Different messages produce different session IDs.
  - The session ID does not contain the raw token.

## Task 3: Remove `vision` from default allowed models

### Requirements

- Remove `"vision"` from `DEFAULT_ALLOWED_MODELS` in `src/router/routing.py` and from `DEFAULT_FALLBACKS`.
- If a request triggers `classify_request` to return `"vision"`, the router should fall back to `"fast"` because the alias is no longer allowed.
- Update any tests that expect `vision` to be allowed.

## Files likely changed

- `src/router/main.py`
- `src/router/routing.py`
- `src/router/tests/test_app.py`
- `src/router/tests/test_routing.py`
- `src/router/tests/test_sessions.py` (or add stable-id tests here)

## Verification

Run the router test suite from `src/`:

```bash
PYTHONPATH=. python3 -m pytest router/tests -q
```

Expected: all existing tests pass plus new tests pass.

## Out of scope

- Config file extraction (Slice 1)
- Streaming passthrough (Slice 2)
- Deeper health checks (Slice 3)
- Virtual keys and allowlists (Slice 4)
- Structured logging (Slice 5)

## Notes

- Keep backward compatibility for `create_app()` signature.
- httpx `ConnectError` and `TimeoutException` should be treated as retryable.
- If the explicit model is not allowed, treat it as if no model was supplied and classify.
- The original `fallback_count` stored in a warm session should be carried forward and updated only when a new fallback actually happens in the current request.

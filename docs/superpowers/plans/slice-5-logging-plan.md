# Slice 5: Structured request metadata logging

## Goal

Add a lightweight structured log line per request with session id hash, selected model, routing reason, status, latency, and fallback count. Keep it grep-friendly for the NAS.

## Global Constraints

- Keep the existing FastAPI router structure and file layout.
- Do not change LiteLLM config or docker-compose.
- All new behavior must be covered by tests in `src/router/tests/`. Existing tests must keep passing.
- Test command: `PYTHONPATH=. python3 -m pytest router/tests -q`
- Follow TDD.
- Do not log full prompts or raw bearer tokens. Only metadata.
- Use Python stdlib `logging`; no new dependencies.
- One log line per request (at INFO level), emitted after the response is built.
- Log format should be a single line, key=value or JSON, easy to grep.

## Requirements

1. Add a module-level logger in `src/router/main.py`: `logger = logging.getLogger("router")`.
2. After building the response in `chat_completions` (both streaming and non-streaming), emit one INFO log line containing:
   - `session_id_hash`: a short SHA-256 prefix (first 12 chars) of the session id (not the raw session id; the session id may already be a hash, but hash again for logging consistency).
   - `model`: the final selected model (`current_model`).
   - `provider_model`: the resolved provider/model string (e.g. `openai/kimi-k2.7-code`), present when the router config maps the alias to a provider/model.
   - `reason`: the original decision reason (`decision.reason`).
   - `status`: the HTTP status code of the final upstream response (for streaming, the status is the streaming response status, typically 200; if an exception occurred and no response, log `status=error`).
   - `latency_ms`: elapsed time from the start of `chat_completions` to response built, in milliseconds (int).
   - `fallback_count`: the number of fallback hops.
   - `fallback_from`: the original model if `fallback_count > 0`, else omit or empty.
3. Format: single line, `key=value` pairs separated by spaces, e.g.:
   `session_id_hash=abc123def456 model=coder provider_model=ollama_chat/kimi-k2.7-code reason=explicit-model status=200 latency_ms=42 fallback_count=1 fallback_from=coder`
4. For the streaming path, log after the streaming response object is created (the latency reflects time to start streaming, not full body transfer — that's fine and intended).
5. For error/exception paths, log `status=error` and still emit the line.
6. Do not log the raw bearer token or any prompt content.

## Tests to add

In `src/router/tests/test_logging.py` (new) or extend `test_app.py`:
- `test_chat_logs_request_metadata`: use `caplog` (pytest's log capture) to capture the INFO line; assert it contains `model=`, `reason=`, `status=`, `latency_ms=`, `fallback_count=`, and `session_id_hash=`.
- `test_chat_log_omits_raw_token`: assert the captured log line does not contain the bearer token string.
- `test_chat_log_on_error_path`: simulate an exception/exhausted fallback; assert a log line with `status=error` or the upstream error status is emitted.

## Notes

- Use `time.perf_counter()` at the start of the handler and compute `latency_ms = int((perf_counter() - start) * 1000)`.
- Use `logging.getLogger("router")`; configure no handlers in the app (let the operator configure logging). Tests use `caplog`.
- Keep the log helper small; a single function `_log_request(...)` is fine.
- Ensure streaming still works: log before returning the StreamingResponse.

## Out of scope

- Virtual keys (Slice 4)
- Cost tracking / spend (P2)
- Cache metrics (P2)

## Files likely changed

- `src/router/main.py`
- `src/router/tests/test_logging.py` (new) or `src/router/tests/test_app.py`

# Slice 2: True SSE streaming passthrough

## Goal

Implement real SSE streaming passthrough for `/v1/chat/completions` when the client sends `stream: true`, instead of buffering the whole upstream response.

## Global Constraints

- Keep the existing FastAPI router structure and file layout.
- Do not change LiteLLM config or docker-compose.
- All new behavior must be covered by tests in `src/router/tests/`. Existing tests must keep passing.
- Test command: `PYTHONPATH=. python3 -m pytest router/tests -q`
- Follow TDD.
- The router must continue to apply the same routing/classification/fallback/session logic before streaming begins. Session writes still happen only after a successful upstream response.
- For streaming, "success" means the upstream returned a 200 with `text/event-stream`. If the upstream returns a non-2xx status before streaming, apply the same fallback retry logic as non-streaming.
- Once streaming has begun (200 received and bytes are flowing), do not retry or fallback mid-stream.

## Requirements

1. **Detect streaming requests** in `chat_completions`: if `body.get("stream")` is truthy, use a streaming passthrough path.
2. **Stream the upstream response** to the client using `StreamingResponse` from `fastapi.responses`:
   - Forward the upstream `content-type` (typically `text/event-stream`).
   - Preserve SSE chunks as they arrive (do not buffer the whole body).
   - Forward bytes incrementally from the upstream `httpx` stream.
3. **Apply gateway headers** (`X-Gateway-Model`, `X-Gateway-Reason`, `X-Gateway-Fallback-Count`, `X-Gateway-Fallback-From`) on the streaming response too.
4. **Session writes for streaming**: after the upstream returns a 200 streaming response, write the session (the model that is streaming). Do not wait for the stream to finish.
5. **Fallback for streaming**: if the upstream returns a retryable non-2xx status (e.g., 503) before any streaming begins, retry against the next fallback alias. Once a 200 streaming response is received, do not retry.
6. **Preserve streaming headers and content type** exactly as upstream sends them, except for the gateway headers you add.

## Tests to add

In `src/router/tests/test_app.py` (or a new `test_streaming.py`):
- `test_chat_stream_passthrough_forwards_chunks`: mock upstream returns 200 with `text/event-stream` and two SSE chunks; assert the client receives both chunks in order and the `content-type` is `text/event-stream`.
- `test_chat_stream_sets_gateway_headers`: assert `X-Gateway-Model` and `X-Gateway-Reason` are present on the streaming response.
- `test_chat_stream_writes_session_after_200`: after a streaming 200, the session store contains the selected model.
- `test_chat_stream_fallback_before_stream_starts`: first upstream returns 503 (non-streaming error), second returns 200 streaming; assert the client receives the stream from the fallback model and `X-Gateway-Fallback-From` is set.

## Notes

- Use `httpx.AsyncClient.stream()` or `client.send(request, stream=True)` to get an incremental byte stream.
- Use `fastapi.responses.StreamingResponse` with an async generator that yields chunks from the upstream stream and closes it when done.
- Be careful to close the upstream response/stream in a `finally` block to avoid connection leaks.
- The mock transport (`httpx.MockTransport`) supports streaming via an `httpx.Response` with a `content` bytes payload; for streaming tests, you can return a response with `content=b"data: ...\n\ndata: ...\n\n"` and the router can iterate it in chunks. Alternatively, use `httpx.Response(200, content=..., headers={"content-type": "text/event-stream"})` and have the router read it in chunks. The test should verify the bytes flow through, not necessarily real network chunking.
- Keep the non-streaming path unchanged.

## Out of scope

- Health/readiness depth (Slice 3)
- Virtual keys (Slice 4)
- Structured logging (Slice 5)
- Docker `curl -N` verification (manual, after tests pass)

## Files likely changed

- `src/router/main.py`
- `src/router/tests/test_app.py` or `src/router/tests/test_streaming.py` (new)

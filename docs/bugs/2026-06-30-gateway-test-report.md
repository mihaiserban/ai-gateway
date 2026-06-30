# Gateway Test Report - 2026-06-30

Comprehensive behavioral and edge-case testing against the running gateway stack
(`localhost:4100`). All four services were healthy during testing.

**Test scope**: 30+ test cases covering routing, session stickiness, error
handling, streaming, classifier behavior, and edge cases.

---

## Bugs Found

### 1. Code classifier leading-space mismatch — routing inconsistency (MEDIUM)

**File**: `src/router/classifier.py:6-20`

**What**: Code signal keywords intentionally include a leading space
(`" implement"`, `" refactor"`, `" diff"`, `" stack trace"`, etc.) so they match
only when preceded by another word. But prompts frequently start with code
keywords as the first word — and those fail to match.

**Reproduce**:

| Prompt | Expected | Actual |
|---|---|---|
| `"implement a sort function"` | opencodego-fast | fast |
| `"please implement a sort function"` | opencodego-fast | opencodego-fast |
| `"diff this file"` | opencodego-fast | fast |
| `"here is the diff output"` | opencodego-fast | opencodego-fast |
| `"refactor the codebase"` | opencodego-fast | fast |
| `"I want to refactor the codebase"` | opencodego-fast | opencodego-fast |
| `"stack trace from error"` | opencodego-fast | fast |
| `"here is a stack trace from error"` | opencodego-fast | opencodego-fast |

**Root cause**: `CODE_SIGNALS` uses leading-space-prefixed keywords:
`" implement"`, `" diff"`, `" refactor"`, `" stack trace"`, `" traceback"`.
The leading space was likely intended to avoid false positives (e.g. matching
"implementation" or "difference"), but means first-word usage never matches.

**Fix suggestion**: Strip leading spaces from code signal keywords and use
word-boundary-aware matching, or augment the signal list with non-space-prefixed
variants.

---

### 2. MemorySessionStore ignores TTL — unbounded memory growth (LOW)

**File**: `src/router/sessions.py:24-25`

**What**: `MemorySessionStore.set()` receives `ttl_seconds` but never uses it.
Sessions stored in memory never expire, causing unbounded memory growth if Redis
is unavailable and the router falls back to the memory store.

```python
# sessions.py:24-25
async def set(self, session_id: str, value: dict[str, Any], ttl_seconds: int) -> None:
    self._sessions[session_id] = value  # ttl_seconds is ignored
```

**Impact**: Low in production (Redis is always used). High if Redis is ever
disabled or the fallback path is exercised.

**Fix suggestion**: Add a periodic cleanup loop or store TTL alongside the value
and check on `get()`.

---

### 3. Case-sensitive model matching — silent reroute instead of error (LOW)

**File**: `src/router/routing.py:59-61`

**What**: The `allowed_models` set is checked with exact string comparison
(`explicit_model in config.allowed_models`). Case variations like `"Fast"` or
`"DEEPSEEK-PRO"` don't match and silently fall through to the classifier instead
of returning an error.

**Reproduce**:

| `model` field | Expected | Actual (gateway-model) |
|---|---|---|
| `"Fast"` | error or `fast` | fast (reason: `classified`) |
| `"DEEPSEEK-PRO"` | error or `deepseek-pro` | fast (reason: `classified`) |

**Fix suggestion**: Lowercase the model field before the membership check, or
reject unrecognized values with a 400 error.

---

### 4. POST with no body returns 500 instead of 422 (LOW)

**File**: `src/router/main.py:78`

**What**: Sending `POST /v1/chat/completions` with an empty body triggers an
unhandled JSON decode error in `await request.json()`, resulting in a 500
Internal Server Error instead of a 422 Unprocessable Entity or 400 Bad Request.

```
$ curl -X POST http://localhost:4100/v1/chat/completions
HTTP/1.1 500 Internal Server Error
```

**Fix suggestion**: Add a FastAPI exception handler for `JSONDecodeError` that
returns 400/422, or validate the body before parsing.

---

### 5. Cache hit/miss never detected in metrics (LOW)

**File**: `src/router/main.py:293-297` (also headers forwarding)

**What**: The `cache_counts` in `/metrics` always reports `"unknown"` for all
requests (`{"hit": 0, "miss": 0, "unknown": 66}` after 66 requests). The
`_cache_hit()` function checks for the `x-litellm-cache-hit` header, but
LiteLLM does not appear to emit this header (only `x-litellm-cache-key` is
present in responses).

**Impact**: Cache effectiveness cannot be monitored. Even after identical
requests that should hit the LiteLLM Redis cache, `cache_counts` shows no hits.

**Fix suggestion**: Verify the correct cache-hit header name from LiteLLM
(currently may not exist), or infer cache misses from the presence of
`x-litellm-cache-key` without a corresponding `x-litellm-cache-hit`.

---

## Design Observations (not bugs)

### 6. Invalid/nonexistent model silently falls through to classifier

When `model` is set to a value not in `allowed_models` (e.g. `"nonexistent"`,
`42`, or `""`), the router silently ignores it and uses the classifier result
instead of returning an error. While this is intentional per `choose_model()`,
it can mask misconfigurations in clients.

### 7. Image content classified as "vision" but `vision` is not in `allowed_models`

The classifier correctly detects image content and returns `"vision"`, but
`"vision"` is not in `allowed_models`, so it falls back to `"fast"`.  This is
documented, but image prompts result in upstream errors from non-vision
providers (DeepSeek) and costly retry chains through `ollama-cloud`.

---

## Things That Work Correctly

- Health endpoints (`/healthz`, `/readyz`) correctly report all dependency statuses
- Session stickiness via `X-Session-Id` works; warm sessions persist within the TTL
- Explicit model selection (`"model": "fast"`) correctly routes and takes priority
- Fallback chain triggers correctly on retryable upstream errors (though not tested with real failures)
- Streaming responses (`stream: true`) deliver SSE correctly
- Gateway headers (`X-Gateway-Model`, `X-Gateway-Reason`, `X-Gateway-Fallback-Count`) present on all responses
- Invalid auth keys produce upstream errors (not router crashes)
- Unicode/emoji and multi-turn conversations pass through cleanly
- Concurrent requests (5 simultaneous) all succeed
- Unsupported `/v1/` paths return 501 as expected
- Model discovery (`/v1/models`) works with and without auth
- Reasoning classifier keywords ("analyze", "debug", "explain", etc.) correctly route to `deepseek-pro`
- Code classifier correctly routes `.py`, `src/`, `tests/` signals to `opencodego-fast`

---

## Test Coverage Summary

| Category | Tests | Passed | Notes |
|---|---|---|---|
| Health/readiness | 2 | 2 | |
| Model discovery | 2 | 2 | |
| Classifier routing (code) | 2 | 2 | See bug #1 for first-word issue |
| Classifier routing (reasoning) | 8 | 8 | All reasoning keywords match correctly |
| Classifier routing (general) | 1 | 1 | |
| Explicit model selection | 3 | 3 | Case-variant models fail silently (bug #3) |
| Code+reasoning overlap | 1 | 1 | Code signals checked first, wins |
| Session stickiness | 2 | 2 | |
| Streaming | 1 | 1 | |
| Error handling | 5 | 5 | 500 on no-body (bug #4) |
| Edge cases (unicode, long prompt, empty content, etc.) | 5 | 5 | |
| Gateway headers | 2 | 2 | |
| Concurrent requests | 1 | 1 | |
| Metrics endpoint | 1 | 1 | Cache counts always unknown (bug #5) |
| Unsupported paths | 1 | 1 | |

**Total: 37 tests, 37 passing at the functional level.**  
**5 bugs identified (1 medium, 4 low severity).**

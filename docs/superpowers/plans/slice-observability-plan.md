# Slice: P2 Observability — pricing metadata and metrics

## Goal

Add `model_info` pricing metadata for aliases so LiteLLM spend tracking is accurate, and add lightweight in-process counters for selected-model and fallback counts surfaced via a small `/metrics` endpoint or in the structured logs.

## Global Constraints

- Keep the existing FastAPI router structure and file layout.
- Do not change docker-compose.
- All new behavior must be covered by tests in `src/router/tests/`. Existing tests must keep passing.
- Test command: `PYTHONPATH=. python3 -m pytest router/tests -q`
- Follow TDD.
- Do not add a new heavy dependency (no prometheus client, etc.). Use stdlib and in-memory counters.
- Pricing metadata goes in `litellm.config.yaml` as `model_info` blocks — that is a LiteLLM config concern, but it is in the repo so we edit it here.
- Keep counters simple: a dict on `app.state` updated per request, read by `/metrics`.

## Requirements

1. **Pricing metadata in `litellm.config.yaml`**
   - Add `model_info` blocks under each alias with `input_cost_per_token` and `output_cost_per_token` where known, using publicly documented pricing for DeepSeek V4, OpenCode Go, and Ollama Cloud. Where pricing is unknown or zero (free tier), set both to `0.0` and add a comment.
   - This is best-effort; exact values should be marked as approximate in a comment.
2. **In-process metrics**
   - Add a `Metrics` helper (in a new `src/router/metrics.py`) with thread-safe-ish in-memory counters:
     - `selected_model_counts: dict[str, int]`
     - `fallback_count_total: int`
     - `requests_total: int`
   - Increment on each `chat_completions` request: `requests_total += 1`, `selected_model_counts[final_model] += 1`, and if `fallback_count > 0`, `fallback_count_total += fallback_count`.
   - Do not store prompts or tokens; only counts.
3. **`/metrics` endpoint**
   - `GET /metrics` returns JSON: `{"requests_total": N, "fallback_count_total": N, "selected_model_counts": {...}}`.
   - This is a basic debug endpoint; no auth required for now (it is on the LAN-only port).
4. **Tests**
   - `test_metrics_counts_requests_and_models`: two requests (different models); assert counts.
   - `test_metrics_counts_fallbacks`: one request with a fallback; assert `fallback_count_total` incremented and `selected_model_counts` reflects the final model.
   - `test_metrics_endpoint_returns_counts`: GET `/metrics` returns the JSON shape.

## Notes

- Pricing values: DeepSeek V4 flash/pro pricing is documented at api-docs.deepseek.com; use approximate per-token values. Ollama Cloud `gemma3:27b` pricing may be zero or metered; set 0.0 with a comment if unknown. OpenCode Go pricing is subscription-based; set 0.0 with a comment.
- Keep the `Metrics` class simple; do not over-engineer. A frozen-ish dataclass with a couple of methods is fine.
- Resetting counters across processes is fine (in-memory only). Document that in a comment.

## Out of scope

- NAS runbook (separate slice)
- Caching headers (separate slice)
- Cache hit/miss tracking (deferred until LiteLLM exposes it reliably)

## Files likely changed

- `src/litellm.config.yaml` (model_info blocks)
- `src/router/metrics.py` (new)
- `src/router/main.py` (wire metrics + /metrics endpoint)
- `src/router/tests/test_metrics.py` (new)
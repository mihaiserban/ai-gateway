# AI Gateway To-Do List

This list comes from reviewing the current plan and implementation after the
first working MVP. The gateway is usable locally, but these items are needed to
turn it into a NAS-ready personal v1.

## Review Findings

- **Fallback behavior is mostly not implemented in the router.**
  `src/router/routing.py` defines `next_fallback()`, but `src/router/main.py`
  never uses it. LiteLLM has fallback config, but the router does not retry,
  classify fallback errors, or report the actual fallback path.
- **Failed upstream requests still update sticky session state.**
  The router writes the chosen model before the LiteLLM call succeeds, so a
  failing model can become sticky.
- **Streaming is not actually streamed.**
  The router buffers upstream responses and returns a normal response. Clients
  using `stream: true` need real SSE passthrough.
- **Fallback session IDs are unstable.**
  The router uses Python `hash()`, which changes between processes. The plan
  calls for a stable hash plus caller key fingerprint.
- **`/healthz` is too shallow.**
  It only reports router health. The plan expects router, LiteLLM, Redis, and
  Postgres/stack readiness visibility.
- **Router config is hard-coded.**
  Aliases, TTL, fallbacks, and classifier keywords live in Python instead of a
  config file.
- **Plan has stale items.**
  `docs/PLAN.md` references `router_config.yaml` and streaming passthrough, but
  the current implementation does not provide those yet.
- **Virtual keys/model allowlists are still pending.**
  The README smoke tests still use the master key. Agents should use LiteLLM
  virtual keys with model allowlists.

## P0: Correctness And Safety

- [x] Move session writes after successful upstream response, or mark failed
      sessions separately.
- [x] Implement router-level fallback retry for selected error classes: `429`,
      `500`, `502`, `503`, `504`, timeout errors, and network errors.
- [x] Do not retry auth/client errors: `400`, `401`, `403`, and most `404`.
- [x] Add tests proving a failed first model retries the next fallback.
- [x] Add tests proving a failed upstream request does not poison sticky session
      state.
- [x] Replace Python `hash()` fallback session IDs with stable SHA-256.
- [x] Include caller key fingerprint in fallback session IDs without storing raw
      bearer tokens.
- [x] Make `vision` unavailable until an actual vision alias exists, or add a
      working vision alias.

## P1: Reliability V1

- [ ] Add configurable request timeouts per alias.
- [ ] Add exponential backoff for router-level retries.
- [ ] Add `X-Gateway-Fallback-From` and `X-Gateway-Fallback-Count` response
      headers.
- [ ] Log structured request metadata: session id hash, selected model, routing
      reason, status, latency, and fallback count.
- [ ] Add LiteLLM and Redis checks to `/healthz`.
- [ ] Add a deeper `/readyz` endpoint for dependency readiness.
- [ ] Add an outage simulation test with mock transport.

## P1: Streaming

- [ ] Implement true SSE streaming passthrough for `/v1/chat/completions`.
- [ ] Preserve streaming headers and content type.
- [ ] Add tests for `stream: true`.
- [ ] Verify streaming through Docker with `curl -N`.

## P1: Config

- [ ] Add `src/router/router_config.yaml`.
- [ ] Move TTL, allowed aliases, fallback chains, timeouts, and classifier
      keywords into config.
- [ ] Validate config at startup.
- [ ] Fail fast if router config references aliases missing from LiteLLM config.
- [ ] Update `docs/PLAN.md` once config exists.

## P1: Auth And Access

- [ ] Create one LiteLLM virtual key per agent/tool.
- [ ] Stop using the master key in runbook examples except admin setup.
- [ ] Add model allowlists per key.
- [ ] Document Codex CLI config using the gateway URL and a virtual key.
- [ ] Add a smoke test using a virtual key, not the master key.

## P2: Cost And Observability

- [ ] Add `model_info` pricing metadata for OpenCode Go, DeepSeek, and Ollama
      Cloud aliases.
- [ ] Track selected model counts.
- [ ] Track fallback counts.
- [ ] Track cache hit/miss when LiteLLM exposes it.
- [ ] Add a daily spend summary command or runbook section.
- [ ] Add a lightweight log format that is easy to grep on the NAS.

## P2: Caching

- [ ] Verify LiteLLM Redis cache hits through logs or API metadata.
- [ ] Add cache-related response headers if available.
- [ ] Add stable `prompt_cache_key` only for providers that support it.
- [ ] Keep semantic caching out until basic cache metrics are proven.

## P2: NAS-Ready

- [ ] Add backup and restore commands for Postgres and Redis volumes.
- [ ] Add update and rollback runbook steps.
- [ ] Add secret rotation runbook steps.
- [ ] Add a `.env` generation helper script.
- [ ] Document Tailscale-only exposure as the default deployment posture.
- [ ] Decide whether LiteLLM admin UI needs a protected maintenance profile.

## P3: Future

- [ ] Copilot Pro spike with OAuth persistence.
- [ ] Optional GitHub Models provider.
- [ ] Optional Z.AI provider.
- [ ] Provider optimization after at least one week of cost and latency data.
- [ ] Portkey comparison spike only if LiteLLM becomes painful.

## Recent Verification

- Router unit tests: `PYTHONPATH=. python3 -m pytest router/tests -q`
- Docker stack health: router, LiteLLM, Postgres, and Redis reported healthy.
- Live gateway smoke test: `http://localhost:4100/v1/chat/completions` returned
  HTTP `200` through the router.

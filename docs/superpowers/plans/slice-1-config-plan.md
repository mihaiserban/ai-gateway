# Slice 1: Config-driven routing

## Goal

Move hard-coded routing configuration out of Python and into `src/router/router_config.yaml`, validate it at startup, and fail fast if the router references aliases missing from the LiteLLM config.

## Global Constraints

- Keep the existing FastAPI router structure and file layout.
- Do not change LiteLLM fallback behavior or `litellm.config.yaml` semantics; only read it for alias validation.
- All new behavior must be covered by tests in `src/router/tests/`. Existing tests must keep passing.
- Test command: `PYTHONPATH=. python3 -m pytest router/tests -q`
- `PyYAML==6.0.2` is already in `requirements.txt`; use it.
- Keep `RouteConfig` dataclass as the in-memory representation; load it from YAML at startup.
- Keep backward compatibility: `create_app()` must still work with defaults when no config file is present (current behavior preserved).
- Follow TDD.

## Requirements

1. **Add `src/router/router_config.yaml`** with these keys (mirroring current defaults):
   - `cache_ttl_seconds: 600`
   - `allowed_models:` list of aliases
   - `fallbacks:` mapping of alias -> list of fallback aliases
   - `timeouts:` per-alias request timeout in seconds (optional; default 120)
   - `classifier_keywords:` with `code_signals` and `reasoning_signals` lists
2. **Add a loader** in `src/router/routing.py` (or a new `src/router/config.py`) that:
   - Reads `router_config.yaml` from a path (default: `src/router/router_config.yaml`, overridable via `ROUTER_CONFIG_PATH` env var).
   - Returns a `RouteConfig` populated from the file.
   - Falls back to the current `RouteConfig()` defaults if the file is missing.
3. **Wire the loader into `create_app()`** so `app.state.route_config` is loaded from config when present.
4. **Validate at startup**:
   - Every alias in `fallbacks` keys must be in `allowed_models`.
   - Every alias referenced in a fallback list must be in `allowed_models`.
   - Fail fast with a clear error message if validation fails.
5. **Cross-check against LiteLLM config**:
   - Parse `src/litellm.config.yaml` and extract the `model_list` model names.
   - Fail fast at startup if any `allowed_models` alias is missing from the LiteLLM `model_list`.
   - The LiteLLM config path should be overridable via `LITELLM_CONFIG_PATH` env var, defaulting to `src/litellm.config.yaml` relative to the router's working directory. Be pragmatic: if the LiteLLM config file is not found, log a warning but do not crash (the router may run in contexts where LiteLLM config is elsewhere).
6. **Move classifier keywords into config**:
   - `classifier.py` should accept configured `code_signals` and `reasoning_signals` instead of module-level constants, while keeping the constants as defaults when no config is provided.
   - `classify_request` signature may take an optional config or signals argument; keep backward compatibility for existing callers/tests.
7. **Update `docs/PLAN.md`** to note that `router_config.yaml` now exists and the stale item is resolved.

## Tests to add

- `tests/test_config.py` (or extend `test_routing.py`):
  - Loading from a YAML file populates `RouteConfig`.
  - Missing config file falls back to defaults.
  - Validation fails when a fallback references an alias not in `allowed_models`.
  - Validation fails when a fallback key is not in `allowed_models`.
  - Cross-check fails when an allowed alias is missing from LiteLLM `model_list` (use a temp LiteLLM config).
  - Cross-check warns but does not crash when LiteLLM config is missing.
- Update classifier tests if the signature changes.

## Files likely changed

- `src/router/router_config.yaml` (new)
- `src/router/routing.py` (loader + validation)
- `src/router/classifier.py` (configurable signals)
- `src/router/main.py` (wire loader into create_app)
- `src/router/tests/test_config.py` (new) or `test_routing.py`
- `docs/PLAN.md` (note config exists)

## Out of scope

- Streaming (Slice 2)
- Health/readiness depth (Slice 3)
- Virtual keys (Slice 4)
- Structured logging (Slice 5)

## Notes

- Keep the `RouteConfig` dataclass frozen and immutable where possible.
- Do not add retry/backoff config to the YAML yet; that is a later reliability item.
- The `timeouts` per-alias field is for future use in Slice 5; loading it is fine but wiring it into httpx client timeout can be deferred. If you wire it, keep the default 120.
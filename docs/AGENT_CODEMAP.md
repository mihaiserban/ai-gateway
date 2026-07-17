# Agent Code Map

This map is for contributors and coding agents that need to find the right
files quickly. Product behavior and operations are documented in
[../README.md](../README.md) and [../src/README.md](../src/README.md).

## Discovery

The codebase-memory MCP project name is:

```text
Users-mitzuuuu-code-personal-agent-ai-gateway
```

Prefer graph tools for code discovery, then fall back to `rg` for string
literals, configs, and docs.

## Change Areas

| Task | Edit here | Test here |
| --- | --- | --- |
| Chat completions, request flow, fallback responses, streaming handoff | `src/router/main.py`, `src/router/routing.py`, `src/router/routing_state.py` | `src/router/tests/test_app.py`, `src/router/tests/test_routing.py`, `src/router/tests/test_reliability.py`, `src/router/tests/test_streaming.py` |
| Health, readiness, dependency status | `src/router/health.py`, `src/router/main.py` | `src/router/tests/test_health.py`, `src/router/tests/test_metrics.py` |
| Session stickiness and Redis fallback | `src/router/sessions.py`, `src/router/main.py` | `src/router/tests/test_sessions.py`, `src/router/tests/test_redis_stats.py` |
| Prompt redaction and logging safety | `src/router/redaction.py`, `src/router/main.py` | `src/router/tests/test_redaction.py`, `src/router/tests/test_logging.py` |
| Gateway config parsing and generated runtime YAML | `src/router/gateway_config.py`, `src/scripts/gateway.py`, `src/scripts/generate_configs.py`, `src/gateway.config.yaml` | `src/router/tests/test_gateway_config.py`, `src/router/tests/test_gateway_config_generator.py`, `src/router/tests/test_gateway_cli.py`, `src/router/tests/test_config.py` |
| Live model catalog and model resolution | `src/router/live_catalog.py`, `src/router/routing.py`, `src/gateway.config.yaml` | `src/router/tests/test_live_catalog.py`, `src/router/tests/test_routing.py` |
| Dashboard data and UI | `src/router/dashboard.py` | `src/router/tests/test_dashboard.py` |
| Prompt-free usage ledger | `src/ledger/main.py`, `src/router/usage_events.py` | `src/ledger/tests/test_main.py`, `src/router/tests/test_usage_events.py`, `src/router/tests/test_usage_event_integration.py` |
| OpenCode client plugin | `src/clients/opencode_plugin/index.js` | `src/clients/opencode_plugin/index.test.mjs` |

## Commands

Run the full local developer check before committing:

```bash
make check
```

Useful narrower checks:

```bash
make test
make test-node
make lint
make format-check
make type
make coverage
```

If `src/gateway.config.yaml` changes, regenerate committed runtime YAML before
testing:

```bash
make regen
```

## Config Rules

- Edit `src/gateway.config.yaml` for providers, connections, combos, router
  knobs, and LiteLLM settings.
- Do not hand-edit `src/litellm.config.yaml` or
  `src/router/router_config.yaml`; regenerate them with `make regen`.
- Use LiteLLM virtual keys for smoke tests. Do not put the master key in agent
  or client configs.

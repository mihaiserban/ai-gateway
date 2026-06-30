# Single Gateway Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `src/gateway.config.yaml` as the only human-edited model/routing configuration and generate the existing router and LiteLLM config files from it.

**Architecture:** Keep the current runtime contract: the router reads `src/router/router_config.yaml`, and LiteLLM reads `src/litellm.config.yaml`. Add a deterministic generator in `src/scripts/generate_configs.py` that reads `src/gateway.config.yaml`, validates it, and writes both runtime files with generated-file headers.

**Tech Stack:** Python 3.12, PyYAML, pytest, existing router test dependencies.

## Global Constraints

- Do not move secrets into committed YAML.
- Keep `src/.env.example` as the secret/runtime environment template.
- Keep `src/docker-compose.yml` as the service topology owner.
- Do not add a runtime dependency beyond existing router/test dependencies.
- Generated files must be deterministic so tests can catch drift.

---

### Task 1: Add Unified Config And Generator

**Files:**
- Create: `src/gateway.config.yaml`
- Create: `src/scripts/generate_configs.py`
- Create: `src/scripts/__init__.py`

**Interfaces:**
- Produces: `load_gateway_config(path: Path) -> dict[str, Any]`
- Produces: `render_router_config(config: dict[str, Any]) -> dict[str, Any]`
- Produces: `render_litellm_config(config: dict[str, Any]) -> dict[str, Any]`
- Produces: `generate(config_path: Path, router_path: Path, litellm_path: Path) -> None`

- [x] **Step 1: Write failing generator tests**

Add tests in `src/router/tests/test_gateway_config_generator.py` that import `src.scripts.generate_configs` and assert generated dictionaries contain router settings, LiteLLM model params, and validation errors.

- [x] **Step 2: Run tests to verify failure**

Run: `PYTHONPATH=. python3 -m pytest src/router/tests/test_gateway_config_generator.py -q`
Expected: FAIL because `src.scripts.generate_configs` does not exist.

- [x] **Step 3: Add `src/gateway.config.yaml`**

Move the human-edited values from `src/router/router_config.yaml` and `src/litellm.config.yaml` into one YAML file with sections for `router`, `litellm`, and `models`.

- [x] **Step 4: Add generator implementation**

Implement loading, validation, rendering, deterministic YAML writing, and a small CLI entrypoint.

- [x] **Step 5: Run generator tests**

Run: `PYTHONPATH=. python3 -m pytest src/router/tests/test_gateway_config_generator.py -q`
Expected: PASS.

### Task 2: Generate Runtime Configs And Drift Test

**Files:**
- Modify: `src/router/router_config.yaml`
- Modify: `src/litellm.config.yaml`
- Modify: `src/router/tests/test_gateway_config_generator.py`
- Modify: `README.md`
- Modify: `src/README.md`

**Interfaces:**
- Consumes: `generate(config_path: Path, router_path: Path, litellm_path: Path) -> None`

- [x] **Step 1: Write failing drift test**

Add a test that renders from `src/gateway.config.yaml` into a temp directory and compares the exact text to committed `src/router/router_config.yaml` and `src/litellm.config.yaml`.

- [x] **Step 2: Run test to verify failure**

Run: `PYTHONPATH=. python3 -m pytest src/router/tests/test_gateway_config_generator.py::test_committed_generated_configs_match_gateway_config -q`
Expected: FAIL until generated files include the deterministic header/output.

- [x] **Step 3: Generate runtime config files**

Run: `python3 src/scripts/generate_configs.py`
Expected: updates both runtime YAML files.

- [x] **Step 4: Document the edit point**

Update README references so humans edit `src/gateway.config.yaml`, not generated runtime files.

- [x] **Step 5: Run focused tests**

Run: `PYTHONPATH=. python3 -m pytest src/router/tests/test_gateway_config_generator.py src/router/tests/test_config.py -q`
Expected: PASS.

### Task 3: Full Verification And Simplification Review

**Files:**
- No new implementation files.

**Interfaces:**
- Consumes: all generated config and tests from Tasks 1 and 2.

- [x] **Step 1: Run full router tests**

Run: `PYTHONPATH=src python3 -m pytest src/router/tests -q`
Expected: PASS.

- [x] **Step 2: Run static checks if dependencies are available**

Run: `python3 -m ruff format --check .`
Expected: PASS.

Observed: `python3 -m ruff format --check .` reports only pre-existing
`src/router/tests/test_virtual_key.py` formatting.

Run: `python3 -m ruff check .`
Expected: PASS.

- [x] **Step 3: Run ponytail review**

Review the resulting diff for unnecessary abstraction or bloat. Apply simplifications that preserve tests and project constraints.

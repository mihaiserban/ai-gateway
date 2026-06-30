# Guardrails Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add lightweight, enforceable quality gates for the Python/FastAPI gateway.

**Architecture:** Centralize Python tool configuration in `pyproject.toml`, keep development tools in `src/router/requirements-dev.txt`, run cheap checks locally through pre-commit, and run the same gates in CI. Do not change router runtime behavior unless the new checks expose a concrete issue.

**Tech Stack:** Python 3.12, FastAPI, pytest, Ruff, mypy, pytest-cov, pip-audit, pre-commit, GitHub Actions.

## Global Constraints

- Keep guardrails focused on this Python service; do not add JS/TS tools such as ESLint or knip.
- Preserve runtime dependency pins unless the dependency audit requires a security bump that passes the full test suite.
- Keep CI commands runnable from the repository root.
- Include Docker Compose config validation for `src/docker-compose.yml`.
- Include dependency and secret-oriented checks without requiring real production secrets.

---

### Task 1: Static Analysis Configuration

**Files:**
- Create: `pyproject.toml`
- Modify: `src/router/requirements-dev.txt`

**Interfaces:**
- Consumes: existing `src/router` package and tests.
- Produces: tool commands `ruff format --check .`, `ruff check .`, `mypy src/router`, and `pytest --cov=router --cov-fail-under=80 src/router/tests`.

- [ ] **Step 1: Write configuration**

Create `pyproject.toml` with Ruff, mypy, pytest, and coverage settings for `src/router`.

- [ ] **Step 2: Add dev tools**

Add `ruff`, `mypy`, `pytest-cov`, `types-PyYAML`, `pip-audit`, `pre-commit`, and `detect-secrets` to `src/router/requirements-dev.txt`.

- [ ] **Step 3: Verify static checks**

Run:

```bash
python3 -m ruff format --check .
python3 -m ruff check .
python3 -m mypy src/router
PYTHONPATH=src python3 -m pytest --cov=router --cov-fail-under=80 src/router/tests
```

Expected: commands exit 0 after any lint/type issues found by the new rules are fixed.

### Task 2: Local And CI Guardrails

**Files:**
- Create: `.pre-commit-config.yaml`
- Create: `.github/workflows/ci.yml`
- Create: `.secrets.baseline`

**Interfaces:**
- Consumes: `pyproject.toml`, `src/router/requirements.txt`, `src/router/requirements-dev.txt`, `src/docker-compose.yml`, and `.secrets.baseline`.
- Produces: repeatable local pre-commit hooks and CI checks.

- [ ] **Step 1: Add pre-commit hooks**

Create `.pre-commit-config.yaml` with hooks for Ruff formatting, Ruff linting, basic file hygiene, YAML validation, and secret scanning via `detect-secrets-hook --baseline .secrets.baseline`.

- [ ] **Step 2: Add CI workflow**

Create `.github/workflows/ci.yml` to install dependencies, run Ruff, mypy, pytest with coverage, pip-audit, `detect-secrets-hook`, and Docker Compose config validation.

- [ ] **Step 3: Verify guardrail commands**

Run:

```bash
python3 -m pre_commit run --all-files
python3 -m pip_audit -r src/router/requirements.txt -r src/router/requirements-dev.txt
docker compose -f src/docker-compose.yml config --quiet
```

Expected: commands exit 0 after any configuration issues are fixed.

### Task 3: Review And Final Verification

**Files:**
- Review: all changed files

**Interfaces:**
- Consumes: final git diff.
- Produces: simplified diff and verification evidence.

- [ ] **Step 1: Run ponytail review**

Review the resulting diff for unnecessary dependencies, speculative configuration, and noisy rules.

- [ ] **Step 2: Apply simplifications**

Remove or relax any guardrail that adds noise without protecting this gateway.

- [ ] **Step 3: Run final verification**

Run the complete guardrail set again:

```bash
python3 -m ruff format --check .
python3 -m ruff check .
python3 -m mypy src/router
PYTHONPATH=src python3 -m pytest --cov=router --cov-fail-under=80 src/router/tests
python3 -m pip_audit -r src/router/requirements.txt -r src/router/requirements-dev.txt
docker compose -f src/docker-compose.yml config --quiet
```

Expected: commands exit 0, or any remaining failures are reported with exact causes.

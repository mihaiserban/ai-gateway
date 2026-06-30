# Model Alias Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Evolve the gateway from provider-specific role aliases into a curated model catalog: task aliases, exact model-family aliases, and provider deployment aliases.

**Architecture:** Keep provider deployments as concrete LiteLLM-backed entries. Add generated aliases for tasks and exact model families that copy a concrete target's LiteLLM parameters while retaining their own public name, timeout, metadata, and fallback chain. Continue generating both `src/router/router_config.yaml` and `src/litellm.config.yaml` from `src/gateway.config.yaml`.

**Tech Stack:** Python 3.12, PyYAML, FastAPI router config, LiteLLM proxy YAML, pytest.

## Global Constraints

- `src/gateway.config.yaml` remains the only human-edited runtime configuration file.
- `python3 src/scripts/generate_configs.py` must regenerate `src/router/router_config.yaml` and `src/litellm.config.yaml`.
- Provider deployments are regular `models` entries with `litellm_model` and provider environment fields.
- Task and model-family aliases are `aliases` entries with `name`, `target`, optional `fallbacks`, optional `timeout`, and optional `model_info`.
- Aliases resolve recursively to a concrete provider deployment for LiteLLM rendering.
- Alias cycles fail config validation before any generated file is written.
- Fallback targets may name provider deployments, model-family aliases, or task aliases, but every target must be a defined entry.
- Task aliases should target exact model-family aliases and should name concrete alternate provider deployments or exact families that resolve away from the task's primary provider. This avoids falling back from a failed Ollama-backed task alias into a broad alias that immediately resolves to Ollama again.
- Exact model-family aliases are first-class direct-selection aliases with provider fallback. For example, `model: deepseek-v4-pro` means "use DeepSeek V4 Pro and let the gateway choose/fallback provider."
- Provider deployment aliases force one backend and have no fallbacks. For example, `model: deepseek-v4-pro-deepseek` means "use the DeepSeek-hosted deployment only."
- `model_info.reasoning_level` is catalog metadata only in this implementation and must be one of `none`, `low`, `medium`, or `high`.
- Do not rewrite request bodies to add provider-specific reasoning parameters in this implementation; OpenAI-style, Anthropic-style, DeepSeek, Ollama, and OpenCode Go reasoning controls are not one shared contract.
- Do not expose the full provider catalog as routable aliases; expose curated task aliases, curated exact model-family aliases such as `glm-5.2` and `deepseek-v4-pro`, and selected provider deployment aliases in `src/gateway.config.yaml`.
- After implementation, invoke `ponytail-review` on the resulting diff and apply simplification suggestions that do not conflict with these requirements.

---

## File Structure

- Modify `src/scripts/generate_configs.py`: add alias parsing, recursive target resolution, alias validation, and rendering of aliases into both runtime configs.
- Modify `src/gateway.config.yaml`: split current aliases into concrete provider deployments plus public task and exact model-family aliases.
- Regenerate `src/router/router_config.yaml`: generated router config should include all curated public aliases and provider deployments.
- Regenerate `src/litellm.config.yaml`: generated LiteLLM config should include a concrete model entry for every routable alias.
- Modify `src/router/tests/test_gateway_config_generator.py`: cover alias rendering, validation errors, and committed generated config parity.
- Modify `README.md`: replace stale default alias table and fallback chains with the new role/family/provider model contract.
- Modify `docs/models.md`: document the curated catalog levels and the active aliases.

---

### Task 1: Add Alias Support To The Config Generator

**Files:**
- Modify: `src/scripts/generate_configs.py`
- Test: `src/router/tests/test_gateway_config_generator.py`

**Interfaces:**
- Consumes: Existing `load_gateway_config(path: Path) -> dict[str, Any]`, `render_router_config(config: dict[str, Any]) -> dict[str, Any]`, and `render_litellm_config(config: dict[str, Any]) -> dict[str, Any]`.
- Produces: `_entries(config: dict[str, Any]) -> list[dict[str, Any]]`, `_entry_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]`, and `_resolve_entry(entry: dict[str, Any], entries: dict[str, dict[str, Any]]) -> dict[str, Any]`.

- [ ] **Step 1: Write failing tests for alias rendering**

Add these tests to `src/router/tests/test_gateway_config_generator.py` after `test_render_litellm_config_from_gateway_config`:

```python
def test_alias_entries_render_to_router_and_litellm_configs():
    config = {
        "router": {"default_model": "coder"},
        "litellm": {
            "settings": {"drop_params": True, "request_timeout": 120, "num_retries": 1},
            "cache": {"type": "redis", "redis_url_env": "REDIS_URL"},
            "general": {
                "master_key_env": "LITELLM_MASTER_KEY",
                "database_url_env": "DATABASE_URL",
            },
        },
        "models": [
            {
                "name": "deepseek-v4-pro-ollama",
                "litellm_model": "ollama_chat/deepseek-v4-pro",
                "api_key_env": "OLLAMA_API_KEY",
                "api_base_env": "OLLAMA_API_BASE",
                "timeout": 120,
                "model_info": {
                    "reasoning_level": "high",
                    "input_cost_per_token": 0.0,
                    "output_cost_per_token": 0.0,
                },
            },
            {
                "name": "deepseek-v4-pro-deepseek",
                "litellm_model": "deepseek/deepseek-v4-pro",
                "api_key_env": "DEEPSEEK_API_KEY",
                "timeout": 120,
                "model_info": {
                    "reasoning_level": "high",
                    "input_cost_per_token": 0.00000028,
                    "output_cost_per_token": 0.00000056,
                },
            },
        ],
        "aliases": [
            {
                "name": "deepseek-v4-pro",
                "target": "deepseek-v4-pro-ollama",
                "fallbacks": ["deepseek-v4-pro-deepseek"],
                "timeout": 120,
                "model_info": {"mode": "model-family", "reasoning_level": "high"},
            },
            {
                "name": "coder",
                "target": "deepseek-v4-pro",
                "fallbacks": ["deepseek-v4-pro-deepseek"],
                "timeout": 120,
                "model_info": {"mode": "role", "reasoning_level": "high"},
            },
        ],
    }

    router_config = render_router_config(config)
    litellm_config = render_litellm_config(config)

    assert router_config["default_model"] == "coder"
    assert router_config["allowed_models"] == [
        "deepseek-v4-pro-ollama",
        "deepseek-v4-pro-deepseek",
        "deepseek-v4-pro",
        "coder",
    ]
    assert router_config["fallbacks"]["deepseek-v4-pro"] == ["deepseek-v4-pro-deepseek"]
    assert router_config["fallbacks"]["deepseek-v4-pro-deepseek"] == []
    assert router_config["fallbacks"]["coder"] == ["deepseek-v4-pro-deepseek"]
    assert router_config["provider_models"]["coder"] == "ollama_chat/deepseek-v4-pro"

    coder_entry = next(entry for entry in litellm_config["model_list"] if entry["model_name"] == "coder")
    assert coder_entry["litellm_params"]["model"] == "ollama_chat/deepseek-v4-pro"
    assert coder_entry["litellm_params"]["api_key"] == "os.environ/OLLAMA_API_KEY"
    assert coder_entry["litellm_params"]["api_base"] == "os.environ/OLLAMA_API_BASE"
    assert coder_entry["model_info"] == {"mode": "role", "reasoning_level": "high"}
```

- [ ] **Step 2: Run the alias rendering test and verify it fails**

Run:

```bash
pytest src/router/tests/test_gateway_config_generator.py::test_alias_entries_render_to_router_and_litellm_configs -v
```

Expected: fail because `render_router_config()` does not include `aliases` and because `_render_model()` expects every entry to have `litellm_model`.

- [ ] **Step 3: Write failing tests for alias validation**

Add these tests to `src/router/tests/test_gateway_config_generator.py` after `test_validation_rejects_unknown_fallback_target`:

```python
def test_validation_rejects_unknown_alias_target(tmp_path):
    config_path = tmp_path / "gateway.config.yaml"
    config_path.write_text(
        """
router:
  default_model: coder
litellm:
  settings:
    drop_params: true
  cache:
    redis_url_env: REDIS_URL
  general:
    master_key_env: LITELLM_MASTER_KEY
    database_url_env: DATABASE_URL
models:
  - name: deepseek-v4-pro-ollama
    litellm_model: ollama_chat/deepseek-v4-pro
    api_key_env: OLLAMA_API_KEY
aliases:
  - name: coder
    target: missing-target
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="alias 'coder' targets unknown entry 'missing-target'"):
        load_gateway_config(config_path)


def test_validation_rejects_alias_cycles(tmp_path):
    config_path = tmp_path / "gateway.config.yaml"
    config_path.write_text(
        """
router:
  default_model: coder
litellm:
  settings:
    drop_params: true
  cache:
    redis_url_env: REDIS_URL
  general:
    master_key_env: LITELLM_MASTER_KEY
    database_url_env: DATABASE_URL
models:
  - name: deepseek-v4-pro-ollama
    litellm_model: ollama_chat/deepseek-v4-pro
    api_key_env: OLLAMA_API_KEY
aliases:
  - name: coder
    target: planner
  - name: planner
    target: coder
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="alias cycle detected: coder -> planner -> coder"):
        load_gateway_config(config_path)


def test_validation_rejects_unknown_reasoning_level(tmp_path):
    config_path = tmp_path / "gateway.config.yaml"
    config_path.write_text(
        """
router:
  default_model: coder
litellm:
  settings:
    drop_params: true
  cache:
    redis_url_env: REDIS_URL
  general:
    master_key_env: LITELLM_MASTER_KEY
    database_url_env: DATABASE_URL
models:
  - name: deepseek-v4-pro-ollama
    litellm_model: ollama_chat/deepseek-v4-pro
    api_key_env: OLLAMA_API_KEY
    model_info:
      reasoning_level: extreme
aliases:
  - name: coder
    target: deepseek-v4-pro-ollama
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="reasoning_level for 'deepseek-v4-pro-ollama'"):
        load_gateway_config(config_path)
```

- [ ] **Step 4: Run validation tests and verify they fail**

Run:

```bash
pytest \
  src/router/tests/test_gateway_config_generator.py::test_validation_rejects_unknown_alias_target \
  src/router/tests/test_gateway_config_generator.py::test_validation_rejects_alias_cycles \
  src/router/tests/test_gateway_config_generator.py::test_validation_rejects_unknown_reasoning_level \
  -v
```

Expected: fail because aliases are not validated yet.

- [ ] **Step 5: Implement alias-aware entry handling**

In `src/scripts/generate_configs.py`, replace calls to `_models(config)` inside `render_router_config()` and `render_litellm_config()` with `_entries(config)` and `_entry_map(config)`.

Use this implementation structure:

```python
def render_router_config(config: dict[str, Any]) -> dict[str, Any]:
    router = _mapping(config, "router")
    entries = _entries(config)
    entries_by_name = _entry_map(config)

    return {
        "cache_ttl_seconds": router.get("cache_ttl_seconds", 600),
        "default_model": router.get("default_model", entries[0]["name"]),
        "retry_base_delay": router.get("retry_base_delay", 0.2),
        "retry_max_delay": router.get("retry_max_delay", 2.0),
        "allowed_models": [entry["name"] for entry in entries],
        "fallbacks": {entry["name"]: list(entry.get("fallbacks") or []) for entry in entries},
        "timeouts": {entry["name"]: entry.get("timeout", 120) for entry in entries},
        "cache_key_aliases": list(router.get("cache_key_aliases") or []),
        "provider_models": {
            entry["name"]: _resolve_entry(entry, entries_by_name)["litellm_model"]
            for entry in entries
        },
    }


def render_litellm_config(config: dict[str, Any]) -> dict[str, Any]:
    litellm = _mapping(config, "litellm")
    settings = _mapping(litellm, "settings")
    cache = _mapping(litellm, "cache")
    general = _mapping(litellm, "general")
    logging = _mapping(litellm, "logging")
    entries = _entries(config)
    entries_by_name = _entry_map(config)

    litellm_settings = {
        "drop_params": settings.get("drop_params", True),
        "request_timeout": settings.get("request_timeout", 120),
        "num_retries": settings.get("num_retries", 1),
        "cache": True,
        "cache_params": {
            "type": cache.get("type", "redis"),
            "redis_url": _env_ref(cache.get("redis_url_env", "REDIS_URL")),
        },
    }
    callbacks = list(logging.get("callbacks") or [])
    if callbacks:
        litellm_settings["callbacks"] = callbacks

    return {
        "model_list": [_render_model(entry, entries_by_name) for entry in entries],
        "litellm_settings": litellm_settings,
        "general_settings": {
            "master_key": _env_ref(general.get("master_key_env", "LITELLM_MASTER_KEY")),
            "database_url": _env_ref(general.get("database_url_env", "DATABASE_URL")),
        },
        "router_settings": {
            "fallbacks": [{entry["name"]: list(entry.get("fallbacks") or [])} for entry in entries],
        },
    }
```

Replace `_render_model()` with this alias-aware version:

```python
def _render_model(entry: dict[str, Any], entries_by_name: dict[str, dict[str, Any]]) -> dict[str, Any]:
    resolved = _resolve_entry(entry, entries_by_name)
    params = {
        "model": resolved["litellm_model"],
        "api_key": _env_ref(resolved["api_key_env"]),
    }
    if api_base_env := resolved.get("api_base_env"):
        params["api_base"] = _env_ref(api_base_env)
    if additional_drop_params := resolved.get("additional_drop_params"):
        params["additional_drop_params"] = list(additional_drop_params)

    rendered = {
        "model_name": entry["name"],
        "litellm_params": params,
    }
    if model_info := entry.get("model_info", resolved.get("model_info")):
        rendered["model_info"] = dict(model_info)
    return rendered
```

Add these helper functions below `_write_yaml()`:

```python
def _entries(config: dict[str, Any]) -> list[dict[str, Any]]:
    return _models(config) + _aliases(config)


def _entry_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["name"]: entry for entry in _entries(config)}


def _aliases(config: dict[str, Any]) -> list[dict[str, Any]]:
    aliases = config.get("aliases") or []
    if not isinstance(aliases, list):
        raise ConfigError("aliases must be a list")
    if not all(isinstance(alias, dict) for alias in aliases):
        raise ConfigError("each alias must be a mapping")
    return aliases


def _validate_model_info(entry: dict[str, Any]) -> None:
    model_info = entry.get("model_info") or {}
    if not isinstance(model_info, dict):
        raise ConfigError(f"model_info for {entry['name']!r} must be a mapping")
    reasoning_level = model_info.get("reasoning_level")
    if reasoning_level is not None and reasoning_level not in {"none", "low", "medium", "high"}:
        raise ConfigError(
            f"reasoning_level for {entry['name']!r} must be one of none, low, medium, high"
        )


def _resolve_entry(
    entry: dict[str, Any],
    entries_by_name: dict[str, dict[str, Any]],
    seen: tuple[str, ...] = (),
) -> dict[str, Any]:
    if "litellm_model" in entry:
        return entry

    name = entry["name"]
    target = entry.get("target")
    if not isinstance(target, str) or not target:
        raise ConfigError(f"alias {name!r} is missing required string field 'target'")
    if target not in entries_by_name:
        raise ConfigError(f"alias {name!r} targets unknown entry {target!r}")
    if target in seen:
        cycle = " -> ".join((*seen, target))
        raise ConfigError(f"alias cycle detected: {cycle}")
    return _resolve_entry(entries_by_name[target], entries_by_name, (*seen, target))
```

- [ ] **Step 6: Update validation to include aliases**

In `_validate(config)`, validate all entry names, concrete model fields, alias targets, fallback targets, and default model:

```python
def _validate(config: dict[str, Any]) -> None:
    router = _mapping(config, "router")
    models = _models(config)
    aliases = _aliases(config)
    entries = models + aliases
    names: set[str] = set()

    for entry in entries:
        name = _required_str(entry, "name", "entry")
        if name in names:
            raise ConfigError(f"duplicate model alias {name!r}")
        names.add(name)
        _validate_model_info(entry)

    for model in models:
        name = model["name"]
        _required_str(model, "litellm_model", name)
        _required_str(model, "api_key_env", name)

    entries_by_name = {entry["name"]: entry for entry in entries}
    for alias in aliases:
        _required_str(alias, "target", f"alias {alias['name']!r}")
        _resolve_entry(alias, entries_by_name, (alias["name"],))

    for entry in entries:
        name = entry["name"]
        for target in entry.get("fallbacks") or []:
            if target not in names:
                raise ConfigError(f"fallback target {target!r} under {name!r} is not a defined model")

    default_model = router.get("default_model", entries[0]["name"])
    if default_model not in names:
        raise ConfigError(f"default_model {default_model!r} is not a defined model")
```

- [ ] **Step 7: Run generator tests**

Run:

```bash
pytest src/router/tests/test_gateway_config_generator.py -v
```

Expected: pass except `test_committed_generated_configs_match_gateway_config` may fail later after changing `src/gateway.config.yaml`; it should pass before Task 2.

- [ ] **Step 8: Commit Task 1**

```bash
git add src/scripts/generate_configs.py src/router/tests/test_gateway_config_generator.py
git commit -m "feat(config): support generated model aliases"
```

---

### Task 2: Migrate Gateway Config To Role, Family, And Provider Layers

**Files:**
- Modify: `src/gateway.config.yaml`
- Regenerate: `src/router/router_config.yaml`
- Regenerate: `src/litellm.config.yaml`
- Test: `src/router/tests/test_gateway_config_generator.py`

**Interfaces:**
- Consumes: Alias support from Task 1.
- Produces: Curated task aliases `explorer`, `planner`, `coder`, `coder-fast`, and `vision`; exact model-family aliases `deepseek-v4-flash`, `deepseek-v4-pro`, `glm-5.2`, `kimi-k2.7-code`, and `kimi-k2.6`; each with catalog-only `model_info.reasoning_level`.

- [ ] **Step 1: Replace the `models:` section with provider deployments**

In `src/gateway.config.yaml`, keep the existing `router` and `litellm` sections. Replace the current `models:` entries with these concrete provider deployment entries:

```yaml
models:
  - name: deepseek-v4-flash-ollama
    litellm_model: ollama_chat/deepseek-v4-flash
    api_base_env: OLLAMA_API_BASE
    api_key_env: OLLAMA_API_KEY
    timeout: 60
    model_info:
      role: provider-deployment
      family: deepseek-v4-flash
      provider: ollama
      reasoning_level: low
      input_cost_per_token: 0.0
      output_cost_per_token: 0.0

  - name: deepseek-v4-flash-deepseek
    litellm_model: deepseek/deepseek-v4-flash
    api_key_env: DEEPSEEK_API_KEY
    timeout: 60
    model_info:
      role: provider-deployment
      family: deepseek-v4-flash
      provider: deepseek
      reasoning_level: low
      input_cost_per_token: 0.00000014
      output_cost_per_token: 0.00000028

  - name: deepseek-v4-flash-opencodego
    litellm_model: openai/deepseek-v4-flash
    api_base_env: OPENCODE_GO_API_BASE
    api_key_env: OPENCODE_GO_API_KEY
    timeout: 60
    additional_drop_params:
      - reasoningSummary
    model_info:
      role: provider-deployment
      family: deepseek-v4-flash
      provider: opencodego
      reasoning_level: low
      input_cost_per_token: 0.0
      output_cost_per_token: 0.0

  - name: glm-5.2-ollama
    litellm_model: ollama_chat/glm-5.2
    api_base_env: OLLAMA_API_BASE
    api_key_env: OLLAMA_API_KEY
    timeout: 120
    model_info:
      role: provider-deployment
      family: glm-5.2
      provider: ollama
      reasoning_level: high
      input_cost_per_token: 0.0
      output_cost_per_token: 0.0

  - name: glm-5.2-opencodego
    litellm_model: openai/glm-5.2
    api_base_env: OPENCODE_GO_API_BASE
    api_key_env: OPENCODE_GO_API_KEY
    timeout: 120
    additional_drop_params:
      - reasoningSummary
    model_info:
      role: provider-deployment
      family: glm-5.2
      provider: opencodego
      reasoning_level: high
      input_cost_per_token: 0.0
      output_cost_per_token: 0.0

  - name: kimi-k2.7-code-ollama
    litellm_model: ollama_chat/kimi-k2.7-code
    api_base_env: OLLAMA_API_BASE
    api_key_env: OLLAMA_API_KEY
    timeout: 120
    model_info:
      role: provider-deployment
      family: kimi-k2.7-code
      provider: ollama
      reasoning_level: medium
      input_cost_per_token: 0.0
      output_cost_per_token: 0.0

  - name: kimi-k2.7-code-opencodego
    litellm_model: openai/kimi-k2.7-code
    api_base_env: OPENCODE_GO_API_BASE
    api_key_env: OPENCODE_GO_API_KEY
    timeout: 120
    additional_drop_params:
      - reasoningSummary
    model_info:
      role: provider-deployment
      family: kimi-k2.7-code
      provider: opencodego
      reasoning_level: medium
      input_cost_per_token: 0.0
      output_cost_per_token: 0.0

  - name: deepseek-v4-pro-ollama
    litellm_model: ollama_chat/deepseek-v4-pro
    api_base_env: OLLAMA_API_BASE
    api_key_env: OLLAMA_API_KEY
    timeout: 120
    model_info:
      role: provider-deployment
      family: deepseek-v4-pro
      provider: ollama
      reasoning_level: high
      input_cost_per_token: 0.0
      output_cost_per_token: 0.0

  - name: deepseek-v4-pro-deepseek
    litellm_model: deepseek/deepseek-v4-pro
    api_key_env: DEEPSEEK_API_KEY
    timeout: 120
    model_info:
      role: provider-deployment
      family: deepseek-v4-pro
      provider: deepseek
      reasoning_level: high
      input_cost_per_token: 0.00000028
      output_cost_per_token: 0.00000056

  - name: kimi-k2.6-ollama
    litellm_model: ollama_chat/kimi-k2.6
    api_base_env: OLLAMA_API_BASE
    api_key_env: OLLAMA_API_KEY
    timeout: 60
    model_info:
      role: provider-deployment
      family: kimi-k2.6
      provider: ollama
      reasoning_level: low
      input_cost_per_token: 0.0
      output_cost_per_token: 0.0

  - name: kimi-k2.6-opencodego
    litellm_model: openai/kimi-k2.6
    api_base_env: OPENCODE_GO_API_BASE
    api_key_env: OPENCODE_GO_API_KEY
    timeout: 120
    additional_drop_params:
      - reasoningSummary
    model_info:
      role: provider-deployment
      family: kimi-k2.6
      provider: opencodego
      reasoning_level: low
      input_cost_per_token: 0.0
      output_cost_per_token: 0.0
```

- [ ] **Step 2: Add the `aliases:` section**

Append this `aliases:` section below `models:` in `src/gateway.config.yaml`:

```yaml
aliases:
  - name: deepseek-v4-flash
    target: deepseek-v4-flash-ollama
    timeout: 60
    fallbacks:
      - deepseek-v4-flash-deepseek
      - deepseek-v4-flash-opencodego
    model_info:
      role: model-family
      family: deepseek-v4-flash
      reasoning_level: low

  - name: glm-5.2
    target: glm-5.2-ollama
    timeout: 120
    fallbacks:
      - glm-5.2-opencodego
      - kimi-k2.7-code-opencodego
    model_info:
      role: model-family
      family: glm-5.2
      reasoning_level: high

  - name: kimi-k2.7-code
    target: kimi-k2.7-code-ollama
    timeout: 120
    fallbacks:
      - kimi-k2.7-code-opencodego
      - deepseek-v4-pro-deepseek
    model_info:
      role: model-family
      family: kimi-k2.7-code
      reasoning_level: medium

  - name: deepseek-v4-pro
    target: deepseek-v4-pro-ollama
    timeout: 120
    fallbacks:
      - deepseek-v4-pro-deepseek
    model_info:
      role: model-family
      family: deepseek-v4-pro
      reasoning_level: high

  - name: kimi-k2.6
    target: kimi-k2.6-ollama
    timeout: 60
    fallbacks:
      - kimi-k2.6-opencodego
    model_info:
      role: model-family
      family: kimi-k2.6
      reasoning_level: low

  - name: explorer
    target: deepseek-v4-flash
    timeout: 60
    fallbacks:
      - deepseek-v4-flash-deepseek
      - deepseek-v4-flash-opencodego
    model_info:
      role: task-alias
      task: explore
      reasoning_level: low

  - name: planner
    target: glm-5.2
    timeout: 120
    fallbacks:
      - glm-5.2-opencodego
      - deepseek-v4-pro-deepseek
      - kimi-k2.7-code-opencodego
    model_info:
      role: task-alias
      task: plan
      reasoning_level: high

  - name: coder
    target: kimi-k2.7-code
    timeout: 120
    fallbacks:
      - kimi-k2.7-code-opencodego
      - deepseek-v4-pro-deepseek
    model_info:
      role: task-alias
      task: build
      reasoning_level: medium

  - name: coder-fast
    target: deepseek-v4-flash
    timeout: 60
    fallbacks:
      - deepseek-v4-flash-deepseek
      - deepseek-v4-flash-opencodego
      - kimi-k2.6-opencodego
    model_info:
      role: task-alias
      task: quick-build
      reasoning_level: low

  - name: vision
    target: kimi-k2.6
    timeout: 120
    fallbacks:
      - kimi-k2.6-opencodego
    model_info:
      role: task-alias
      task: vision
      reasoning_level: medium
```

- [ ] **Step 3: Regenerate runtime configs**

Run:

```bash
python3 src/scripts/generate_configs.py
```

Expected: command exits with status 0 and rewrites `src/router/router_config.yaml` and `src/litellm.config.yaml`.

- [ ] **Step 4: Inspect generated router aliases**

Run:

```bash
python3 - <<'PY'
import yaml
from pathlib import Path
config = yaml.safe_load(Path("src/router/router_config.yaml").read_text())
print(config["default_model"])
print(config["allowed_models"])
print(config["fallbacks"]["coder"])
print(config["provider_models"]["coder"])
PY
```

Expected output:

```text
coder
['deepseek-v4-flash-ollama', 'deepseek-v4-flash-deepseek', 'deepseek-v4-flash-opencodego', 'glm-5.2-ollama', 'glm-5.2-opencodego', 'kimi-k2.7-code-ollama', 'kimi-k2.7-code-opencodego', 'deepseek-v4-pro-ollama', 'deepseek-v4-pro-deepseek', 'kimi-k2.6-ollama', 'kimi-k2.6-opencodego', 'deepseek-v4-flash', 'glm-5.2', 'kimi-k2.7-code', 'deepseek-v4-pro', 'kimi-k2.6', 'explorer', 'planner', 'coder', 'coder-fast', 'vision']
['kimi-k2.7-code-opencodego', 'deepseek-v4-pro-deepseek']
ollama_chat/kimi-k2.7-code
```

- [ ] **Step 5: Run generator parity tests**

Run:

```bash
pytest src/router/tests/test_gateway_config_generator.py -v
```

Expected: pass, including `test_committed_generated_configs_match_gateway_config`.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/gateway.config.yaml src/router/router_config.yaml src/litellm.config.yaml src/router/tests/test_gateway_config_generator.py
git commit -m "config: expose task and model aliases"
```

---

### Task 3: Document The Gateway Model Contract

**Files:**
- Modify: `README.md`
- Modify: `docs/models.md`

**Interfaces:**
- Consumes: Active aliases from Task 2.
- Produces: User-facing documentation that explains which alias level callers should use.

- [ ] **Step 1: Update README routing behavior**

In `README.md`, replace the stale "Default aliases" table and fallback YAML under "Routing Behavior" with:

```markdown
Default public aliases:

| Alias level | Examples | Intended use |
| --- | --- | --- |
| Task aliases | `explorer`, `planner`, `coder`, `coder-fast`, `vision` | Default interface for agents and orchestrators. |
| Model-family aliases | `deepseek-v4-flash`, `deepseek-v4-pro`, `glm-5.2`, `kimi-k2.7-code`, `kimi-k2.6` | Caller wants an exact model family and allows provider fallback. |
| Provider deployment aliases | `deepseek-v4-pro-ollama`, `deepseek-v4-pro-deepseek`, `kimi-k2.7-code-opencodego` | Caller wants to force one provider with no gateway fallback. |

The recommended default for package and orchestrator setup is to use task
aliases first. For example, an orchestrator should map planning to `planner`,
building to `coder`, quick edits to `coder-fast`, and search/simple work to
`explorer`. Packages that need direct model selection can request an exact
model-family alias such as `deepseek-v4-pro`, `deepseek-v4-flash`,
`kimi-k2.7-code`, or `kimi-k2.6`. Provider deployment aliases are available as
an escape hatch when a caller needs to force one backend.

The selection rule is: choose a task alias when you know the job, choose a
model-family alias when you know the model and want provider fallback, and
choose a provider deployment alias only when debugging or forcing one backend.

Fallback chains live in `src/gateway.config.yaml` and are generated into the
router and LiteLLM runtime configs. Task aliases target the preferred exact
model-family alias, but their fallback chains use concrete alternate provider
deployments so a provider outage does not immediately route back to the same
provider.

The catalog also records `reasoning_level` as guidance for humans and packages:
`low`, `medium`, or `high`. This field does not rewrite request parameters.
```

- [ ] **Step 2: Update `docs/models.md` role table**

Replace the "Model roles" section in `docs/models.md` with:

```markdown
## Model contract

The gateway exposes three levels of aliases:

| Level | Examples | Use when |
| --- | --- | --- |
| Task alias | `explorer`, `planner`, `coder`, `coder-fast`, `vision` | A tool or orchestrator wants the gateway's recommended default for a job. |
| Model-family alias | `deepseek-v4-flash`, `deepseek-v4-pro`, `glm-5.2`, `kimi-k2.7-code`, `kimi-k2.6` | A caller wants an exact model family with provider fallback. |
| Provider deployment alias | `deepseek-v4-pro-ollama`, `deepseek-v4-pro-deepseek`, `kimi-k2.7-code-opencodego` | A caller needs to force or debug one provider deployment with no fallback. |

`model_info.reasoning_level` is catalog metadata with values `none`, `low`,
`medium`, or `high`. It is not translated into provider-specific request
parameters by the router.

Recommended orchestrator mapping:

| Orchestrator role | Gateway alias | Reasoning level |
| --- | --- | --- |
| Explore/search/simple work | `explorer` | `low` |
| Plan/reason/analyze | `planner` | `high` |
| Build/code | `coder` | `medium` |
| Quick edits/commits | `coder-fast` | `low` |
| Image input | `vision` | `medium` |
```

- [ ] **Step 3: Update `docs/models.md` active alias table**

Replace the current "Active gateway model aliases" table with one generated from `src/gateway.config.yaml`. The table must include these columns:

```markdown
| Alias | Alias level | Reasoning level | Target/provider model | Timeout (s) | Fallbacks |
| --- | --- | --- | --- | --- | --- |
```

Use `role` from each entry's `model_info` as the alias level and `reasoning_level` from `model_info` as the reasoning level. For aliases, use the resolved provider model from `src/router/router_config.yaml` as `Target/provider model`. For provider deployments, use their `litellm_model`.

- [ ] **Step 4: Update `docs/models.md` fallback section**

Replace the fallback tree with this shape:

```text
explorer
  -> deepseek-v4-flash-deepseek
  -> deepseek-v4-flash-opencodego

planner
  -> glm-5.2-opencodego
  -> deepseek-v4-pro-deepseek
  -> kimi-k2.7-code-opencodego

coder
  -> kimi-k2.7-code-opencodego
  -> deepseek-v4-pro-deepseek

coder-fast
  -> deepseek-v4-flash-deepseek
  -> deepseek-v4-flash-opencodego
  -> kimi-k2.6-opencodego

vision
  -> kimi-k2.6-opencodego

```

- [ ] **Step 5: Commit Task 3**

```bash
git add README.md docs/models.md
git commit -m "docs: explain gateway model alias contract"
```

---

### Task 4: Verify, Simplify, And Prepare For Handoff

**Files:**
- Modify only files required by verification fixes.

**Interfaces:**
- Consumes: Tasks 1 through 3.
- Produces: Passing tests, generated config parity, and a simplified diff reviewed for avoidable complexity.

- [ ] **Step 1: Run focused tests**

Run:

```bash
pytest src/router/tests/test_gateway_config_generator.py src/router/tests/test_config.py src/router/tests/test_routing.py -v
```

Expected: all selected tests pass.

- [ ] **Step 2: Run full test suite**

Run:

```bash
pytest
```

Expected: all tests pass.

- [ ] **Step 3: Verify generated configs are current**

Run:

```bash
python3 src/scripts/generate_configs.py
git diff -- src/router/router_config.yaml src/litellm.config.yaml
```

Expected: no diff for the generated config files after regeneration.

- [ ] **Step 4: Run static checks**

Run:

```bash
ruff check .
mypy src
```

Expected: both commands exit with status 0.

- [ ] **Step 5: Invoke ponytail-review on the diff**

Use the `ponytail-review` skill on the implementation diff. Apply suggestions that remove indirection or duplication without weakening alias validation, generated config parity, or the public curated model contract.

- [ ] **Step 6: Re-run verification after simplification**

Run:

```bash
pytest
ruff check .
mypy src
python3 src/scripts/generate_configs.py
git diff -- src/router/router_config.yaml src/litellm.config.yaml
```

Expected: tests and checks pass, and generated config files remain unchanged after regeneration.

- [ ] **Step 7: Commit verification fixes**

If Step 5 or Step 6 changed files, run:

```bash
git add src/scripts/generate_configs.py src/router/tests/test_gateway_config_generator.py src/gateway.config.yaml src/router/router_config.yaml src/litellm.config.yaml README.md docs/models.md
git commit -m "chore: simplify model alias catalog implementation"
```

If Step 5 and Step 6 changed no files, do not create an empty commit.

---

## Self-Review

- Spec coverage: The plan covers task aliases, exact model-family aliases, provider deployment aliases, recursive target resolution, fallback validation, catalog-only reasoning levels, config migration, generated router and LiteLLM configs, docs, tests, and completion review.
- Red-flag scan: No unresolved planning markers are present.
- Type consistency: The plan uses `dict[str, Any]` consistently with the existing generator and preserves existing public generator function names.

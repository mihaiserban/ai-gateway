from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

SRC_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = SRC_ROOT / "gateway.config.yaml"
DEFAULT_ROUTER_PATH = SRC_ROOT / "router" / "router_config.yaml"
DEFAULT_LITELLM_PATH = SRC_ROOT / "litellm.config.yaml"

HEADER = "# Generated from src/gateway.config.yaml. Do not edit directly.\n\n"


class ConfigError(ValueError):
    """Raised when gateway.config.yaml cannot produce valid runtime configs."""


def load_gateway_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}
    if not isinstance(config, dict):
        raise ConfigError("gateway config must be a YAML mapping")
    _validate(config)
    return config


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
            entry["name"]: _resolve_entry(entry, entries_by_name)["litellm_model"] for entry in entries
        },
        "model_prices": {
            entry["name"]: price
            for entry in entries
            if (price := _price_info(_resolve_entry(entry, entries_by_name))) is not None
        },
    }


def _price_info(entry: dict[str, Any]) -> dict[str, float] | None:
    model_info = entry.get("model_info") or {}
    input_cost = model_info.get("input_cost_per_token")
    output_cost = model_info.get("output_cost_per_token")
    if input_cost is None or output_cost is None:
        return None
    return {
        "input_cost_per_token": float(input_cost),
        "output_cost_per_token": float(output_cost),
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


def generate(
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    router_path: Path = DEFAULT_ROUTER_PATH,
    litellm_path: Path = DEFAULT_LITELLM_PATH,
) -> None:
    config = load_gateway_config(config_path)
    _write_yaml(router_path, render_router_config(config))
    _write_yaml(litellm_path, render_litellm_config(config))


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


# Alias and entry helpers below this line


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
        raise ConfigError(f"reasoning_level for {entry['name']!r} must be one of none, low, medium, high")


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


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    rendered = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    path.write_text(HEADER + rendered, encoding="utf-8")


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


def _models(config: dict[str, Any]) -> list[dict[str, Any]]:
    models = config.get("models")
    if not isinstance(models, list) or not models:
        raise ConfigError("models must be a non-empty list")
    if not all(isinstance(model, dict) for model in models):
        raise ConfigError("each model must be a mapping")
    return models


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a mapping")
    return value


def _required_str(data: dict[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{label} is missing required string field {key!r}")
    return value


def _env_ref(name: Any) -> str:
    if not isinstance(name, str) or not name:
        raise ConfigError("environment variable names must be non-empty strings")
    return f"os.environ/{name}"


if __name__ == "__main__":
    generate()

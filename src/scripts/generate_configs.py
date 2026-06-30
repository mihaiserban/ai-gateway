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
    models = _models(config)

    return {
        "cache_ttl_seconds": router.get("cache_ttl_seconds", 600),
        "default_model": router.get("default_model", models[0]["name"]),
        "retry_base_delay": router.get("retry_base_delay", 0.2),
        "retry_max_delay": router.get("retry_max_delay", 2.0),
        "allowed_models": [model["name"] for model in models],
        "fallbacks": {model["name"]: list(model.get("fallbacks") or []) for model in models},
        "timeouts": {model["name"]: model.get("timeout", 120) for model in models},
        "cache_key_aliases": list(router.get("cache_key_aliases") or []),
        "provider_models": {model["name"]: model["litellm_model"] for model in models},
    }


def render_litellm_config(config: dict[str, Any]) -> dict[str, Any]:
    litellm = _mapping(config, "litellm")
    settings = _mapping(litellm, "settings")
    cache = _mapping(litellm, "cache")
    general = _mapping(litellm, "general")
    logging = _mapping(litellm, "logging")
    models = _models(config)

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
        "model_list": [_render_model(model) for model in models],
        "litellm_settings": litellm_settings,
        "general_settings": {
            "master_key": _env_ref(general.get("master_key_env", "LITELLM_MASTER_KEY")),
            "database_url": _env_ref(general.get("database_url_env", "DATABASE_URL")),
        },
        "router_settings": {
            "fallbacks": [{model["name"]: list(model.get("fallbacks") or [])} for model in models],
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


def _render_model(model: dict[str, Any]) -> dict[str, Any]:
    params = {
        "model": model["litellm_model"],
        "api_key": _env_ref(model["api_key_env"]),
    }
    if api_base_env := model.get("api_base_env"):
        params["api_base"] = _env_ref(api_base_env)
    if additional_drop_params := model.get("additional_drop_params"):
        params["additional_drop_params"] = list(additional_drop_params)

    rendered = {
        "model_name": model["name"],
        "litellm_params": params,
    }
    if model_info := model.get("model_info"):
        rendered["model_info"] = dict(model_info)
    return rendered


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    rendered = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    path.write_text(HEADER + rendered, encoding="utf-8")


def _validate(config: dict[str, Any]) -> None:
    router = _mapping(config, "router")
    models = _models(config)
    names: set[str] = set()
    for model in models:
        name = _required_str(model, "name", "model")
        if name in names:
            raise ConfigError(f"duplicate model alias {name!r}")
        names.add(name)
        _required_str(model, "litellm_model", name)
        _required_str(model, "api_key_env", name)

    for model in models:
        name = model["name"]
        for target in model.get("fallbacks") or []:
            if target not in names:
                raise ConfigError(f"fallback target {target!r} under {name!r} is not a defined model")

    default_model = router.get("default_model", models[0]["name"])
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

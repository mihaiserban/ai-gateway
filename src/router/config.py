from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from router.routing import DEFAULT_ALLOWED_MODELS, DEFAULT_FALLBACKS, RouteConfig

logger = logging.getLogger("router.config")


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "router_config.yaml"
DEFAULT_LITELLM_PATH = Path(__file__).resolve().parent.parent / "litellm.config.yaml"


class ConfigValidationError(ValueError):
    """Raised when router configuration fails validation."""


def load_route_config(config_path: str | None = None) -> RouteConfig:
    """Load a :class:`RouteConfig` from YAML, falling back to defaults."""
    path = Path(config_path) if config_path else Path(os.environ.get("ROUTER_CONFIG_PATH", DEFAULT_CONFIG_PATH))

    if not path.exists():
        logger.warning("router config not found at %s; using defaults", path)
        return RouteConfig()

    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    return _route_config_from_dict(data)


def _route_config_from_dict(data: dict[str, Any]) -> RouteConfig:
    cache_ttl = int(data.get("cache_ttl_seconds", 600))
    default_model = str(data.get("default_model", "fast"))
    allowed = set(data.get("allowed_models") or DEFAULT_ALLOWED_MODELS)
    fallbacks = dict(data.get("fallbacks") or DEFAULT_FALLBACKS)
    timeouts = dict(data.get("timeouts") or {})
    retry_base_delay = float(data.get("retry_base_delay", 0.2))
    retry_max_delay = float(data.get("retry_max_delay", 2.0))
    cache_key_aliases = list(data.get("cache_key_aliases") or [])

    return RouteConfig(
        cache_ttl_seconds=cache_ttl,
        default_model=default_model,
        allowed_models=allowed,
        fallbacks=fallbacks,
        timeouts=timeouts,
        retry_base_delay=retry_base_delay,
        retry_max_delay=retry_max_delay,
        cache_key_aliases=cache_key_aliases,
    )


def validate_route_config(config: RouteConfig) -> None:
    """Fail fast if fallbacks reference aliases outside ``allowed_models``."""
    allowed = config.allowed_models
    if config.default_model not in allowed:
        raise ConfigValidationError(f"default_model {config.default_model!r} is not in allowed_models")

    for key, targets in config.fallbacks.items():
        if key not in allowed:
            raise ConfigValidationError(f"fallback key {key!r} is not in allowed_models")
        for target in targets:
            if target not in allowed:
                raise ConfigValidationError(f"fallback target {target!r} (under {key!r}) is not in allowed_models")


def cross_check_litellm(config: RouteConfig, litellm_path: str | None = None) -> None:
    """Fail fast if an allowed alias is missing from the LiteLLM model list.

    If the LiteLLM config file is missing, warn but do not crash.
    """
    path = Path(litellm_path) if litellm_path else Path(os.environ.get("LITELLM_CONFIG_PATH", DEFAULT_LITELLM_PATH))

    if not path.exists():
        logger.warning("litellm config not found at %s; skipping cross-check", path)
        return

    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    model_list = data.get("model_list") or []
    litellm_names: set[str] = set()
    for entry in model_list:
        if isinstance(entry, dict):
            name = entry.get("model_name")
            if isinstance(name, str):
                litellm_names.add(name)

    missing = config.allowed_models - litellm_names
    if missing:
        raise ConfigValidationError("allowed_models missing from litellm model_list: " + ", ".join(sorted(missing)))


def load_and_validate(
    config_path: str | None = None,
    litellm_path: str | None = None,
) -> RouteConfig:
    """Load, validate, and cross-check a RouteConfig in one step."""
    config = load_route_config(config_path=config_path)
    validate_route_config(config)
    cross_check_litellm(config, litellm_path=litellm_path)
    return config

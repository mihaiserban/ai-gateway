from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from router.routing import ComboRuntime, DeploymentRuntime, RouteConfig

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
    default_model = str(data.get("default_model", RouteConfig.default_model))
    retry_base_delay = float(data.get("retry_base_delay", 0.2))
    retry_max_delay = float(data.get("retry_max_delay", 2.0))
    max_concurrent_upstream = int(data.get("max_concurrent_upstream", 0))
    quota_cooldown_seconds = int(data.get("quota_cooldown_seconds", 300))

    catalog_raw = data.get("catalog") or {}
    catalog_default_view = str(catalog_raw.get("default_view", "all")) if isinstance(catalog_raw, dict) else "all"

    combos = _load_combos(data.get("combos") or {})
    deployments = _load_deployments(data.get("deployments") or {})
    registry_models = _load_registry_models(data.get("registry_models") or {})
    required_env = {dep_id: list(dep.required_env) for dep_id, dep in deployments.items()}

    return RouteConfig(
        cache_ttl_seconds=cache_ttl,
        default_model=default_model,
        retry_base_delay=retry_base_delay,
        retry_max_delay=retry_max_delay,
        max_concurrent_upstream=max_concurrent_upstream,
        quota_cooldown_seconds=quota_cooldown_seconds,
        catalog_default_view=catalog_default_view,
        combos=combos,
        deployments=deployments,
        registry_models=registry_models,
        required_env=required_env,
    )


def _load_combos(raw: dict[str, Any]) -> dict[str, ComboRuntime]:
    combos: dict[str, ComboRuntime] = {}
    for combo_id, body in raw.items():
        if not isinstance(body, dict):
            continue
        strategy = str(body.get("strategy", "score"))
        candidates_raw = body.get("candidates") or []
        candidates = tuple(str(c) for c in candidates_raw) if isinstance(candidates_raw, list) else ()
        task_raw = body.get("task")
        task = str(task_raw) if isinstance(task_raw, str) else None
        combos[combo_id] = ComboRuntime(strategy=strategy, candidates=candidates, task=task)
    return combos


def _load_deployments(raw: dict[str, Any]) -> dict[str, DeploymentRuntime]:
    deployments: dict[str, DeploymentRuntime] = {}
    for deployment_id, body in raw.items():
        if not isinstance(body, dict):
            continue
        required_env_raw = body.get("required_env") or []
        required_env = tuple(str(e) for e in required_env_raw) if isinstance(required_env_raw, list) else ()
        display_name = body.get("display_name")
        if not isinstance(display_name, str):
            display_name = None
        capabilities_raw = body.get("capabilities") or []
        capabilities = tuple(str(c) for c in capabilities_raw) if isinstance(capabilities_raw, list) else ()
        context_length_raw = body.get("context_length")
        if isinstance(context_length_raw, int) and not isinstance(context_length_raw, bool):
            context_length: int | None = context_length_raw
        else:
            context_length = None
        input_cost = _optional_float(body.get("input_cost_per_token"))
        output_cost = _optional_float(body.get("output_cost_per_token"))
        deployments[deployment_id] = DeploymentRuntime(
            provider=str(body.get("provider", "")),
            connection=str(body.get("connection", "")),
            model=str(body.get("model", "")),
            required_env=required_env,
            display_name=display_name,
            capabilities=capabilities,
            context_length=context_length,
            input_cost_per_token=input_cost,
            output_cost_per_token=output_cost,
        )
    return deployments


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _load_registry_models(raw: dict[str, Any]) -> dict[str, list[str]]:
    registry: dict[str, list[str]] = {}
    for model_id, deployment_ids in raw.items():
        if isinstance(deployment_ids, list):
            registry[model_id] = [str(d) for d in deployment_ids]
        else:
            registry[model_id] = []
    return registry


def cross_check_litellm(config: RouteConfig, litellm_path: str | None = None) -> None:
    """Fail fast if a configured deployment is missing from the LiteLLM model list.

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

    missing = set(config.deployments) - litellm_names
    if missing:
        raise ConfigValidationError("deployments missing from litellm model_list: " + ", ".join(sorted(missing)))


def load_and_validate(
    config_path: str | None = None,
    litellm_path: str | None = None,
) -> RouteConfig:
    """Load and cross-check a RouteConfig in one step."""
    config = load_route_config(config_path=config_path)
    cross_check_litellm(config, litellm_path=litellm_path)
    return config

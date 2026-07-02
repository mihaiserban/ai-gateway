from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

# Allow running as a standalone script (no PYTHONPATH set) by exposing the
# sibling `router` package on sys.path. Under pytest `pythonpath=["src"]` this
# is a no-op.
_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from router.gateway_config import Deployment, GatewayCatalog, ScoringWeights, load_gateway_catalog  # noqa: E402

SRC_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = SRC_ROOT / "gateway.config.yaml"
DEFAULT_ROUTER_PATH = SRC_ROOT / "router" / "router_config.yaml"
DEFAULT_LITELLM_PATH = SRC_ROOT / "litellm.config.yaml"

HEADER = "# Generated from src/gateway.config.yaml. Do not edit directly.\n\n"


class ConfigError(ValueError):
    """Raised when gateway.config.yaml cannot produce valid runtime configs."""


def load_gateway_config(path: Path) -> GatewayCatalog:
    """Load and validate the gateway catalog from a YAML file path."""
    return load_gateway_catalog(path)


def render_router_config(catalog: GatewayCatalog) -> dict[str, Any]:
    router = dict(catalog.router) if catalog.router else {}
    return {
        "cache_ttl_seconds": router.get("cache_ttl_seconds", 600),
        "default_model": router.get("default_model", "coder"),
        "retry_base_delay": router.get("retry_base_delay", 0.2),
        "retry_max_delay": router.get("retry_max_delay", 2.0),
        "max_concurrent_upstream": router.get("max_concurrent_upstream", 0),
        "quota_cooldown_seconds": router.get("quota_cooldown_seconds", 300),
        "catalog": {"default_view": "all"},
        "combos": _render_combos(catalog),
        "deployments": _render_deployments(catalog),
        "registry_models": _render_registry_models(catalog),
    }


def _render_combos(catalog: GatewayCatalog) -> dict[str, Any]:
    rendered: dict[str, Any] = {}
    for combo_id, combo in catalog.combos.items():
        candidates = [_deployment_id_for(catalog, c.connection_id, c.model_id) for c in combo.candidates]
        entry: dict[str, Any] = {
            "strategy": combo.strategy,
            "candidates": candidates,
        }
        if combo.task is not None:
            entry["task"] = combo.task
        entry["scoring"] = _render_scoring(combo.scoring)
        rendered[combo_id] = entry
    return rendered


def _render_scoring(weights: ScoringWeights) -> dict[str, float]:
    return {
        "health": weights.health,
        "latency": weights.latency,
        "quota": weights.quota,
        "stability": weights.stability,
        "connection_density": weights.connection_density,
        "priority": weights.priority,
    }


def _deployment_id_for(catalog: GatewayCatalog, connection_id: str, model_id: str) -> str:
    """Resolve a combo candidate's canonical deployment id from the catalog.

    Combo candidates reference (connection, model) pairs; the canonical
    deployment id lives in `catalog.deployments`. Reconstructing it inline would
    risk silent drift if the construction rule ever changes, so we look it up
    and assert membership instead.
    """
    deployment_id = f"{connection_id}.{model_id}"
    if deployment_id not in catalog.deployments:
        raise ConfigError(
            f"combo candidate ({connection_id!r}, {model_id!r}) has no matching deployment {deployment_id!r}"
        )
    return deployment_id


def _render_deployments(catalog: GatewayCatalog) -> dict[str, Any]:
    rendered: dict[str, Any] = {}
    for deployment_id, deployment in catalog.deployments.items():
        required_env = _required_env_for(deployment.api_base_env, deployment.api_key_env)
        entry: dict[str, Any] = {
            "provider": deployment.provider_id,
            "connection": deployment.connection_id,
            "model": deployment.model_id,
            "required_env": required_env,
        }
        if deployment.display_name is not None:
            entry["display_name"] = deployment.display_name
        if deployment.capabilities:
            entry["capabilities"] = list(deployment.capabilities)
        if deployment.context_length is not None:
            entry["context_length"] = deployment.context_length
        if deployment.input_cost_per_token is not None:
            entry["input_cost_per_token"] = deployment.input_cost_per_token
        if deployment.output_cost_per_token is not None:
            entry["output_cost_per_token"] = deployment.output_cost_per_token
        if deployment.priority != 100:
            entry["priority"] = deployment.priority
        if deployment.stability != 0.8:
            entry["stability"] = deployment.stability
        if deployment.max_concurrent is not None:
            entry["max_concurrent"] = deployment.max_concurrent
        rendered[deployment_id] = entry
    return rendered


def _render_registry_models(catalog: GatewayCatalog) -> dict[str, list[str]]:
    registry: dict[str, list[str]] = {}
    for deployment_id, deployment in catalog.deployments.items():
        registry.setdefault(deployment.model_id, []).append(deployment_id)
    return registry


def _required_env_for(api_base_env: str | None, api_key_env: str | None) -> list[str]:
    env: list[str] = []
    if api_base_env:
        env.append(api_base_env)
    if api_key_env:
        env.append(api_key_env)
    return env


def render_litellm_config(catalog: GatewayCatalog) -> dict[str, Any]:
    litellm = dict(catalog.litellm) if catalog.litellm else {}
    settings = _mapping(litellm, "settings")
    cache = _mapping(litellm, "cache")
    general = _mapping(litellm, "general")
    logging_settings = _mapping(litellm, "logging")

    litellm_settings: dict[str, Any] = {
        "drop_params": settings.get("drop_params", True),
        "request_timeout": settings.get("request_timeout", 120),
        "num_retries": settings.get("num_retries", 1),
        "cache": True,
        "cache_params": {
            "type": cache.get("type", "redis"),
            "redis_url": _env_ref(cache.get("redis_url_env", "REDIS_URL")),
        },
    }
    callbacks = list(logging_settings.get("callbacks") or [])
    if callbacks:
        litellm_settings["callbacks"] = callbacks

    return {
        "model_list": [_render_model(deployment) for deployment in catalog.deployments.values()],
        "litellm_settings": litellm_settings,
        "general_settings": {
            "master_key": _env_ref(general.get("master_key_env", "LITELLM_MASTER_KEY")),
            "database_url": _env_ref(general.get("database_url_env", "DATABASE_URL")),
        },
    }


def _render_model(deployment: Deployment) -> dict[str, Any]:
    params: dict[str, Any] = {
        "model": deployment.litellm_model,
    }
    if deployment.api_base_env:
        params["api_base"] = _env_ref(deployment.api_base_env)
    if deployment.api_key_env:
        params["api_key"] = _env_ref(deployment.api_key_env)
    if deployment.drop_params:
        params["additional_drop_params"] = list(deployment.drop_params)

    return {
        "model_name": deployment.id,
        "litellm_params": params,
        "model_info": {
            "provider": deployment.provider_id,
            "connection": deployment.connection_id,
            "model": deployment.model_id,
        },
    }


def generate(
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    router_path: Path = DEFAULT_ROUTER_PATH,
    litellm_path: Path = DEFAULT_LITELLM_PATH,
) -> None:
    catalog = load_gateway_config(config_path)
    _write_yaml(router_path, render_router_config(catalog))
    _write_yaml(litellm_path, render_litellm_config(catalog))


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    rendered = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    path.write_text(HEADER + rendered, encoding="utf-8")


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a mapping")
    return value


def _env_ref(name: Any) -> str:
    if not isinstance(name, str) or not name:
        raise ConfigError("environment variable names must be non-empty strings")
    return f"os.environ/{name}"


if __name__ == "__main__":
    generate()

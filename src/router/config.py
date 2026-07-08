from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from router.routing import ComboRuntime, DeploymentRuntime, RouteConfig, ScoringWeights, TierRuntime

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
    )


def _load_combos(raw: dict[str, Any]) -> dict[str, ComboRuntime]:
    combos: dict[str, ComboRuntime] = {}
    for combo_id, body in raw.items():
        if not isinstance(body, dict):
            continue
        strategy = str(body.get("strategy", "score"))
        candidates_raw = body.get("candidates") or []
        candidates = tuple(str(c) for c in candidates_raw) if isinstance(candidates_raw, list) else ()
        scoring = _load_scoring(body.get("scoring"))
        task_raw = body.get("task")
        task = str(task_raw) if isinstance(task_raw, str) else None
        tiers = _load_tiers(body.get("tiers"))
        combos[combo_id] = ComboRuntime(
            strategy=strategy,
            candidates=candidates,
            task=task,
            scoring=scoring,
            tiers=tiers,
        )
    return combos


def _load_tiers(raw: Any) -> dict[str, TierRuntime]:
    if raw is None or not isinstance(raw, Mapping):
        return {}
    tiers: dict[str, TierRuntime] = {}
    for tier_id, tier_body in raw.items():
        if not isinstance(tier_body, dict):
            continue
        tier_candidates_raw = tier_body.get("candidates")
        tier_candidates: tuple[str, ...] | None = None
        if isinstance(tier_candidates_raw, list):
            tier_candidates = tuple(str(c) for c in tier_candidates_raw)
        tier_strategy = tier_body.get("strategy")
        tier_strategy_val = str(tier_strategy) if isinstance(tier_strategy, str) else None
        tier_scoring = _load_scoring(tier_body.get("scoring"))
        task_raw = tier_body.get("task")
        tier_task = str(task_raw) if isinstance(task_raw, str) else None
        tiers[tier_id] = TierRuntime(
            id=tier_id,
            candidates=tier_candidates,
            strategy=tier_strategy_val,
            scoring=tier_scoring,
            task=tier_task,
        )
    return tiers


def _load_scoring(raw: Any) -> ScoringWeights | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        return None
    kwargs: dict[str, float] = {}
    for key, default in (
        ("health", 0.30),
        ("latency", 0.20),
        ("quota", 0.15),
        ("stability", 0.15),
        ("connection_density", 0.10),
        ("priority", 0.10),
    ):
        value = raw.get(key, default)
        if isinstance(value, int | float) and not isinstance(value, bool):
            kwargs[key] = float(value)
        else:
            kwargs[key] = default
    return ScoringWeights(**kwargs)


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
            priority=_int_or_default(body, "priority", 100),
            stability=_float_or_default(body, "stability", 0.8),
            max_concurrent=_optional_int(body, "max_concurrent"),
        )
    return deployments


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _optional_int(data: Mapping[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _int_or_default(data: Mapping[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _float_or_default(data: Mapping[str, Any], key: str, default: float) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
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

"""Live model catalog built from `RouteConfig` and the current environment.

The `/v1/models` endpoint exposes three kinds of entries:

* `combo`         - curated public models backed by candidate deployments.
* `registry-model` - a registry model id (e.g. ``kimi-k2.7-code``) expanded from
                     every active deployment that serves it, with aggregated
                     metadata (providers, connections, capabilities, ...).
* `connection-model` - a single deployment id (e.g. ``ollama-cloud.kimi-k2.7-code``)
                      exposed under its own id.

Deployments whose ``required_env`` variables are missing from the runtime
environment are hidden, as are combos with no active candidates and registry
models with no active deployments.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from router.routing import ComboRuntime, DeploymentRuntime, RouteConfig

__all__ = [
    "active_deployment_ids",
    "build_live_model_catalog",
    "deployment_is_active",
]

_VALID_VIEWS = ("all", "combos", "registry", "connections")


def deployment_is_active(deployment: DeploymentRuntime, env: Mapping[str, str]) -> tuple[bool, list[str]]:
    """Return ``(active, missing_env)`` for a deployment against ``env``."""
    missing = [name for name in deployment.required_env if name not in env]
    return (not missing, missing)


def active_deployment_ids(config: RouteConfig, env: Mapping[str, str]) -> set[str]:
    """Return the set of deployment ids whose ``required_env`` is satisfied."""
    return {dep_id for dep_id, dep in config.deployments.items() if deployment_is_active(dep, env)[0]}


def build_live_model_catalog(
    config: RouteConfig,
    view: str = "all",
    env: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Build the live model catalog for the ``/v1/models`` endpoint."""
    if view not in _VALID_VIEWS:
        raise ValueError(f"Unknown catalog view {view!r}. Use all, combos, registry, or connections.")

    resolved_env = env if env is not None else os.environ
    active = active_deployment_ids(config, resolved_env)

    combos = _build_combo_entries(config, active) if view in ("all", "combos") else []
    registry = _build_registry_entries(config, active) if view in ("all", "registry") else []
    connections = _build_connection_entries(config, active) if view in ("all", "connections") else []

    return combos + registry + connections


def _build_combo_entries(config: RouteConfig, active: set[str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for combo_id, combo in config.combos.items():
        candidates = [c for c in combo.candidates if c in active]
        if candidates:
            entries.append(_combo_model_entry(combo_id, combo, candidates, active, config))
        for tier_id, tier in combo.tiers.items():
            tier_candidates = tier.candidates if tier.candidates is not None else combo.candidates
            tier_active = [c for c in tier_candidates if c in active]
            if not tier_active:
                continue
            tier_id_full = f"{combo_id}:{tier_id}"
            tier_combo = ComboRuntime(
                strategy=tier.strategy if tier.strategy is not None else combo.strategy,
                candidates=tuple(tier_active),
                task=tier.task if tier.task is not None else combo.task,
                scoring=tier.scoring if tier.scoring is not None else combo.scoring,
            )
            entries.append(_combo_model_entry(tier_id_full, tier_combo, tier_active, active, config))
    return entries


def _combo_model_entry(
    model_id: str,
    combo: ComboRuntime,
    active_candidates: list[str],
    active: set[str],
    config: RouteConfig,
) -> dict[str, Any]:
    gateway: dict[str, Any] = {
        "kind": "combo",
        "strategy": combo.strategy,
        "candidates": list(active_candidates),
    }
    context_lengths: list[int] = []
    for candidate in active_candidates:
        deployment = config.deployments.get(candidate)
        if deployment is not None and deployment.context_length is not None:
            context_lengths.append(deployment.context_length)
    if context_lengths:
        gateway["context_length"] = min(context_lengths)
    if combo.task is not None:
        gateway["task"] = combo.task
    return _base_entry(model_id, gateway)


def _build_registry_entries(config: RouteConfig, active: set[str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for model_id in sorted(config.registry_models):
        deployment_ids = [d for d in config.registry_models[model_id] if d in active]
        if not deployment_ids:
            continue
        deployments = [config.deployments[d] for d in deployment_ids if d in config.deployments]
        if not deployments:
            continue
        providers = sorted({dep.provider for dep in deployments})
        connections = sorted({dep.connection for dep in deployments})
        capabilities = sorted({cap for dep in deployments for cap in dep.capabilities})
        context_lengths = [dep.context_length for dep in deployments if dep.context_length is not None]
        input_costs = [dep.input_cost_per_token for dep in deployments if dep.input_cost_per_token is not None]
        output_costs = [dep.output_cost_per_token for dep in deployments if dep.output_cost_per_token is not None]

        gateway: dict[str, Any] = {
            "kind": "registry-model",
            "model": model_id,
            "providers": providers,
            "connections": connections,
            "deployments": sorted(deployment_ids),
        }
        if capabilities:
            gateway["capabilities"] = capabilities
        if context_lengths:
            gateway["context_length"] = min(context_lengths)
        pricing = _min_pricing(input_costs, output_costs)
        if pricing is not None:
            gateway["pricing"] = pricing
        entries.append(_base_entry(model_id, gateway))
    return entries


def _build_connection_entries(config: RouteConfig, active: set[str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for deployment_id in sorted(active):
        deployment = config.deployments.get(deployment_id)
        if deployment is None:
            continue
        gateway: dict[str, Any] = {
            "kind": "connection-model",
            "provider": deployment.provider,
            "connection": deployment.connection,
            "model": deployment.model,
        }
        if deployment.capabilities:
            gateway["capabilities"] = list(deployment.capabilities)
        if deployment.context_length is not None:
            gateway["context_length"] = deployment.context_length
        entries.append(_base_entry(deployment_id, gateway))
    return entries


def _base_entry(model_id: str, gateway: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": model_id,
        "object": "model",
        "owned_by": "gateway",
        "gateway": gateway,
    }


def _min_pricing(input_costs: list[float], output_costs: list[float]) -> dict[str, float] | None:
    if not input_costs and not output_costs:
        return None
    pricing: dict[str, float] = {}
    if input_costs:
        pricing["input_cost_per_token"] = min(input_costs)
    if output_costs:
        pricing["output_cost_per_token"] = min(output_costs)
    return pricing or None

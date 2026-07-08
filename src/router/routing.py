from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from router.routing_state import GatewayRoutingState

DEFAULT_TIMEOUT_SECONDS = 120

DEFAULT_SCORING = {
    "health": 0.30,
    "latency": 0.20,
    "quota": 0.15,
    "stability": 0.15,
    "connection_density": 0.10,
    "priority": 0.10,
}


@dataclass(frozen=True)
class ScoringWeights:
    health: float = DEFAULT_SCORING["health"]
    latency: float = DEFAULT_SCORING["latency"]
    quota: float = DEFAULT_SCORING["quota"]
    stability: float = DEFAULT_SCORING["stability"]
    connection_density: float = DEFAULT_SCORING["connection_density"]
    priority: float = DEFAULT_SCORING["priority"]


@dataclass
class TierRuntime:
    id: str
    candidates: tuple[str, ...] | None = None
    strategy: str | None = None
    scoring: ScoringWeights | None = None
    task: str | None = None


@dataclass
class ComboRuntime:
    """Runtime representation of a combo, with candidate deployment ids."""

    strategy: str = "score"
    candidates: tuple[str, ...] = ()
    task: str | None = None
    scoring: ScoringWeights | None = None
    tiers: dict[str, TierRuntime] = field(default_factory=dict)


@dataclass
class DeploymentRuntime:
    """Runtime representation of a configured deployment.

    Mutable so routing-state tests can adjust ``max_concurrent`` in place.
    """

    provider: str
    connection: str
    model: str
    required_env: tuple[str, ...] = ()
    display_name: str | None = None
    capabilities: tuple[str, ...] = ()
    context_length: int | None = None
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None
    priority: int = 100
    stability: float = 0.8
    max_concurrent: int | None = None


@dataclass(frozen=True)
class RouteConfig:
    cache_ttl_seconds: int = 600
    default_model: str = "coder"
    retry_base_delay: float = 0.2
    retry_max_delay: float = 2.0
    max_concurrent_upstream: int = 0
    quota_cooldown_seconds: int = 300
    catalog_default_view: str = "all"
    combos: dict[str, ComboRuntime] = field(default_factory=dict)
    deployments: dict[str, DeploymentRuntime] = field(default_factory=dict)
    registry_models: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedModel:
    """The outcome of resolving a client model request against the live catalog.

    ``kind`` is one of ``combo``, ``registry-model``, ``connection-model``,
    ``unavailable`` (known id but no active deployments), or ``not-found``.
    ``ordered_deployments`` is the candidate deployment ids after active
    filtering and strategy ordering; empty for ``unavailable``/``not-found``.
    """

    kind: str
    ordered_deployments: list[str]
    requested_model: str


def is_retryable_failure(status: int | str) -> bool:
    """Return True when a failed attempt should fall back to the next deployment.

    Int status: retryable for {402, 408, 409, 425, 429, 500, 502, 503, 504}.
    402 (Payment Required) = provider billing issue, not a caller error.
    Str status: retryable when it mentions transport/timeout/quota/rate/
    overloaded/unavailable. Caller errors (400/401/403/404/422) never retry.
    """
    if isinstance(status, int):
        return status in {402, 408, 409, 425, 429, 500, 502, 503, 504}
    lowered = str(status).lower()
    return any(word in lowered for word in ("transport", "timeout", "quota", "rate", "overloaded", "unavailable"))


def resolve_model_request(
    model: str | None,
    config: RouteConfig,
    state: GatewayRoutingState,
    now: float,
    env: Mapping[str, str] | None = None,
) -> ResolvedModel:
    """Resolve a client-supplied model id to an ordered candidate deployment list.

    Resolution order:
      1. explicit combo id -> combo candidates (active-filtered, ordered)
      2. explicit deployment id (connection.model) -> forced single deployment
      3. explicit registry model id -> all active deployments serving it
      4. missing/null model -> default_model resolution (recurse)
      5. unknown explicit model -> kind="not-found"
    A resolution with no active deployments becomes kind="unavailable".
    """
    from router.live_catalog import active_deployment_ids

    resolved_env = env if env is not None else os.environ
    active = active_deployment_ids(config, resolved_env)

    requested = model if isinstance(model, str) and model else None
    if requested is None:
        return resolve_model_request(config.default_model, config, state, now, env=env)

    # 1. combo (with optional tier, e.g. "coder:fast")
    combo_id, tier_name = _parse_combo_tier(requested)
    combo = config.combos.get(combo_id)
    if combo is not None:
        tier = combo.tiers.get(tier_name) if tier_name else None
        if tier_name and tier is None:
            return ResolvedModel(kind="not-found", ordered_deployments=[], requested_model=requested)
        tier_candidates = tier.candidates if tier and tier.candidates is not None else combo.candidates
        candidates = [c for c in tier_candidates if c in active]
        if not candidates:
            return ResolvedModel(kind="unavailable", ordered_deployments=[], requested_model=requested)
        effective = ComboRuntime(
            strategy=tier.strategy if tier and tier.strategy is not None else combo.strategy,
            candidates=tuple(candidates),
            task=tier.task if tier and tier.task is not None else combo.task,
            scoring=tier.scoring if tier and tier.scoring is not None else combo.scoring,
        )
        ordered = _order_candidates(list(effective.candidates), effective, config, state, now)
        return ResolvedModel(kind="combo", ordered_deployments=ordered, requested_model=requested)

    # 2. connection-model (explicit deployment id)
    if requested in config.deployments:
        if requested in active:
            return ResolvedModel(kind="connection-model", ordered_deployments=[requested], requested_model=requested)
        return ResolvedModel(kind="unavailable", ordered_deployments=[], requested_model=requested)

    # 3. registry model
    deployment_ids = config.registry_models.get(requested)
    if deployment_ids is not None:
        candidates = [d for d in deployment_ids if d in active]
        if not candidates:
            return ResolvedModel(kind="unavailable", ordered_deployments=[], requested_model=requested)
        ordered = _order_candidates(candidates, None, config, state, now)
        return ResolvedModel(kind="registry-model", ordered_deployments=ordered, requested_model=requested)

    # 5. unknown
    return ResolvedModel(kind="not-found", ordered_deployments=[], requested_model=requested)


def _order_candidates(
    candidate_ids: list[str],
    combo: ComboRuntime | None,
    config: RouteConfig,
    state: GatewayRoutingState,
    now: float,
) -> list[str]:
    """Order candidate deployment ids by the combo strategy (score or priority)."""
    deployments = config.deployments
    if combo is not None and combo.strategy == "priority":
        # Priority strategy keeps configured order, with cooled deployments last.
        cooled: list[str] = []
        live: list[str] = []
        for dep_id in candidate_ids:
            if state.in_quota_cooldown(dep_id, now):
                cooled.append(dep_id)
            else:
                live.append(dep_id)
        return live + cooled

    weights = combo.scoring if combo is not None and combo.scoring is not None else None
    return state.order_deployments(candidate_ids, deployments, weights, now)


def _parse_combo_tier(model: str) -> tuple[str, str | None]:
    if ":" in model:
        base, tier = model.split(":", 1)
        return base, tier
    return model, None

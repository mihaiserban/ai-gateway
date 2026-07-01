from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class ComboRuntime:
    """Runtime representation of a combo, with candidate deployment ids."""

    strategy: str = "score"
    candidates: tuple[str, ...] = ()
    task: str | None = None


@dataclass(frozen=True)
class DeploymentRuntime:
    """Runtime representation of a configured deployment."""

    provider: str
    connection: str
    model: str
    required_env: tuple[str, ...] = ()
    display_name: str | None = None
    capabilities: tuple[str, ...] = ()
    context_length: int | None = None
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None


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
    required_env: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class RouteDecision:
    model: str
    reason: str


def _timeout_for(config: RouteConfig, model: str) -> int:
    """Return the per-deployment timeout, falling back to the default when unset."""
    _ = config
    _ = model
    return DEFAULT_TIMEOUT_SECONDS


def choose_model(
    request: dict[str, Any],
    *,
    session: dict[str, Any] | None,
    now: float,
    config: RouteConfig,
) -> RouteDecision:
    explicit_model = request.get("model")
    if isinstance(explicit_model, str):
        normalized_model = explicit_model.lower()
        if normalized_model in config.combos or normalized_model in config.deployments:
            return RouteDecision(model=normalized_model, reason="explicit-model")

    if session and _is_warm(session, now, config.cache_ttl_seconds):
        session_model = session.get("model")
        if isinstance(session_model, str) and (session_model in config.combos or session_model in config.deployments):
            return RouteDecision(model=session_model, reason="warm-session")

    return RouteDecision(model=config.default_model, reason="default-model")


def next_fallback(model: str, fallback_count: int, config: RouteConfig) -> str | None:
    combo = config.combos.get(model)
    if combo is None:
        return None
    candidates = combo.candidates
    if fallback_count < 0 or fallback_count >= len(candidates):
        return None
    return candidates[fallback_count]


def _is_warm(session: dict[str, Any], now: float, ttl_seconds: int) -> bool:
    try:
        last_used = float(session["last_used_ts"])
    except (KeyError, TypeError, ValueError):
        return False
    return now - last_used < ttl_seconds

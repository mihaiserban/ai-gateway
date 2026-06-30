from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_ALLOWED_MODELS = {
    "explorer",
    "explorer-ds",
    "explorer-ocg",
    "planner",
    "planner-ocg",
    "coder",
    "coder-ocg",
    "coder-dsp",
    "coder-dsp-ds",
    "coder-fast",
    "coder-fast-k26",
    "vision",
    "vision-ocg",
}

DEFAULT_FALLBACKS = {
    "explorer": ["explorer-ds", "explorer-ocg"],
    "explorer-ds": ["explorer-ocg"],
    "planner": ["planner-ocg", "coder"],
    "planner-ocg": ["coder"],
    "coder": ["coder-ocg", "coder-dsp", "coder-dsp-ds"],
    "coder-ocg": ["coder-dsp", "coder-dsp-ds"],
    "coder-dsp": ["coder-dsp-ds"],
    "coder-fast": ["coder-fast-k26", "coder"],
    "coder-fast-k26": ["coder"],
    "vision": ["vision-ocg", "coder"],
    "vision-ocg": ["coder"],
}


DEFAULT_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class RouteConfig:
    cache_ttl_seconds: int = 600
    default_model: str = "coder"
    allowed_models: set[str] = field(default_factory=lambda: set(DEFAULT_ALLOWED_MODELS))
    fallbacks: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_FALLBACKS))
    timeouts: dict[str, int] = field(default_factory=dict)
    retry_base_delay: float = 0.2
    retry_max_delay: float = 2.0
    cache_key_aliases: list[str] = field(default_factory=list)
    provider_models: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RouteDecision:
    model: str
    reason: str


def _timeout_for(config: RouteConfig, model: str) -> int:
    """Return the per-alias timeout, falling back to the default when unset."""
    return config.timeouts.get(model, DEFAULT_TIMEOUT_SECONDS)


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
        if normalized_model in config.allowed_models:
            return RouteDecision(model=normalized_model, reason="explicit-model")

    if session and _is_warm(session, now, config.cache_ttl_seconds):
        session_model = session.get("model")
        if isinstance(session_model, str) and session_model in config.allowed_models:
            return RouteDecision(model=session_model, reason="warm-session")

    return RouteDecision(model=config.default_model, reason="default-model")


def next_fallback(model: str, fallback_count: int, config: RouteConfig) -> str | None:
    candidates = config.fallbacks.get(model, [])
    if fallback_count < 0 or fallback_count >= len(candidates):
        return None
    return candidates[fallback_count]


def _is_warm(session: dict[str, Any], now: float, ttl_seconds: int) -> bool:
    try:
        last_used = float(session["last_used_ts"])
    except (KeyError, TypeError, ValueError):
        return False
    return now - last_used < ttl_seconds

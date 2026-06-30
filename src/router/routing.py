from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from router.classifier import CODE_SIGNALS, REASONING_SIGNALS, classify_request


DEFAULT_ALLOWED_MODELS = {
    "fast",
    "deepseek-pro",
    "opencodego-fast",
    "opencodego-code",
    "ollama-cloud",
}

DEFAULT_FALLBACKS = {
    "fast": ["ollama-cloud"],
    "deepseek-pro": ["opencodego-code", "fast"],
    "opencodego-fast": ["fast", "deepseek-pro"],
    "opencodego-code": ["deepseek-pro", "fast"],
    "ollama-cloud": ["fast"],
}


DEFAULT_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class RouteConfig:
    cache_ttl_seconds: int = 600
    allowed_models: set[str] = field(default_factory=lambda: set(DEFAULT_ALLOWED_MODELS))
    fallbacks: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_FALLBACKS))
    timeouts: dict[str, int] = field(default_factory=dict)
    classifier_keywords: dict[str, list[str]] = field(default_factory=dict)
    retry_base_delay: float = 0.2
    retry_max_delay: float = 2.0
    cache_key_aliases: list[str] = field(default_factory=list)


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
    if isinstance(explicit_model, str) and explicit_model in config.allowed_models:
        return RouteDecision(model=explicit_model, reason="explicit-model")

    if session and _is_warm(session, now, config.cache_ttl_seconds):
        session_model = session.get("model")
        if isinstance(session_model, str) and session_model in config.allowed_models:
            return RouteDecision(model=session_model, reason="warm-session")

    classified = _classify(request, config)
    if classified not in config.allowed_models:
        classified = "fast"
    return RouteDecision(model=classified, reason="classified")


def _classify(request: dict[str, Any], config: RouteConfig) -> str:
    keywords = config.classifier_keywords
    code = tuple(keywords.get("code_signals") or CODE_SIGNALS)
    reasoning = tuple(keywords.get("reasoning_signals") or REASONING_SIGNALS)
    return classify_request(request, code_signals=code, reasoning_signals=reasoning)


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

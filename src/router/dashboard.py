from __future__ import annotations

from typing import Any


SUPPORTED_WINDOWS = {1, 7, 30}
DEFAULT_WINDOW_DAYS = 30


def parse_days(value: str | None) -> int:
    try:
        days = int(value or DEFAULT_WINDOW_DAYS)
    except ValueError:
        return DEFAULT_WINDOW_DAYS
    return days if days in SUPPORTED_WINDOWS else DEFAULT_WINDOW_DAYS


def live_payload(app_state: Any, health: dict[str, str], readiness: dict[str, str]) -> dict[str, Any]:
    config = app_state.route_config
    return {
        "health": health,
        "readiness": readiness,
        "metrics": app_state.metrics.snapshot(),
        "config": {
            "default_model": config.default_model,
            "allowed_models": sorted(config.allowed_models),
            "fallbacks": {key: list(value) for key, value in sorted(config.fallbacks.items())},
            "provider_models": dict(sorted(config.provider_models.items())),
        },
    }

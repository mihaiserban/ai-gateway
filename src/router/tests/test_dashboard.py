from types import SimpleNamespace

import pytest

from router.dashboard import live_payload, parse_days
from router.metrics import Metrics
from router.routing import RouteConfig


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 30),
        ("", 30),
        ("1", 1),
        ("7", 7),
        ("30", 30),
        ("999", 30),
        ("nope", 30),
    ],
)
def test_parse_days_allows_only_supported_windows(value, expected):
    assert parse_days(value) == expected


def test_live_payload_combines_health_metrics_and_config():
    metrics = Metrics()
    metrics.record("coder", "coder", 0, cache_hit=True)
    state = SimpleNamespace(
        metrics=metrics,
        route_config=RouteConfig(
            default_model="coder",
            allowed_models={"coder", "planner"},
            fallbacks={"coder": ["planner"], "planner": []},
            provider_models={"coder": "ollama_chat/kimi-k2.7-code"},
        ),
    )

    payload = live_payload(
        state,
        health={"router": "ok", "litellm": "ok", "redis": "ok", "postgres": "ok", "status": "ok"},
        readiness={"router": "ok", "litellm": "ok", "redis": "ok", "postgres": "ok", "status": "ready"},
    )

    assert payload["health"]["status"] == "ok"
    assert payload["readiness"]["status"] == "ready"
    assert payload["metrics"]["requests_total"] == 1
    assert payload["metrics"]["cache_counts"] == {"hit": 1, "miss": 0, "unknown": 0}
    assert payload["config"]["default_model"] == "coder"
    assert payload["config"]["allowed_models"] == ["coder", "planner"]
    assert payload["config"]["fallbacks"] == {"coder": ["planner"], "planner": []}
    assert payload["config"]["provider_models"] == {"coder": "ollama_chat/kimi-k2.7-code"}

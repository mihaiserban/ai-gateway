from types import SimpleNamespace

import httpx
import psycopg
import pytest

from router.dashboard import UsageSummaryStore, live_payload, parse_days
from router.main import create_app
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


class FakeRedisStatsCollector:
    async def snapshot(self) -> dict[str, object]:
        return {"enabled": False}


@pytest.mark.asyncio
async def test_live_payload_combines_health_metrics_and_config():
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
        redis_stats_collector=FakeRedisStatsCollector(),
    )

    payload = await live_payload(
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
    assert payload["redis"]["enabled"] is False


def test_usage_summary_store_reads_ledger_with_window_filter():
    class FakeCursor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...]]] = []
            self.results: list[list[dict[str, object]]] = [
                [
                    {
                        "requests": 4,
                        "prompt_tokens": 30,
                        "completion_tokens": 20,
                        "total_tokens": 50,
                        "estimated_cost_usd": 1.25,
                        "avg_latency_ms": 125.0,
                        "fallback_count": 2,
                        "cache_hits": 1,
                        "cache_misses": 2,
                        "cache_unknown": 1,
                    }
                ],
                [
                    {
                        "served_model": "coder",
                        "requests": 3,
                        "prompt_tokens": 20,
                        "completion_tokens": 10,
                        "total_tokens": 30,
                        "estimated_cost_usd": 1.0,
                        "avg_latency_ms": 100.0,
                    }
                ],
                [{"day": "2026-06-30", "requests": 4, "total_tokens": 50, "estimated_cost_usd": 1.25}],
                [{"key_hash": "abc123", "requests": 4, "total_tokens": 50, "estimated_cost_usd": 1.25}],
                [
                    {
                        "timestamp": 1782820800.0,
                        "served_model": "coder",
                        "provider_model": "ollama_chat/kimi-k2.7-code",
                        "status": "503",
                        "error_class": "http_503",
                        "latency_ms": 250,
                        "fallback_count": 1,
                    }
                ],
            ]

        def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
            self.calls.append((sql, params or ()))

        def fetchall(self) -> list[dict[str, object]]:
            return self.results.pop(0)

    class FakeConnection:
        def __init__(self) -> None:
            self.cursor_obj = FakeCursor()

        def __enter__(self) -> "FakeConnection":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def cursor(self, *args, **kwargs) -> FakeCursor:
            return self.cursor_obj

    fake = FakeConnection()
    store = UsageSummaryStore("postgresql://example", connect=lambda _, **kwargs: fake)

    summary = store.summary(30)

    assert summary["enabled"] is True
    assert summary["period_days"] == 30
    assert summary["totals"]["requests"] == 4
    assert summary["top_models"][0]["served_model"] == "coder"
    assert summary["daily_usage"][0]["day"] == "2026-06-30"
    assert summary["top_keys"][0]["key_hash"] == "abc123"
    assert summary["recent_failures"][0]["status"] == "503"
    assert len(fake.cursor_obj.calls) == 6
    schema_call = fake.cursor_obj.calls[0]
    assert "create table if not exists gateway_usage_events" in schema_call[0].lower()
    assert schema_call[1] == ()
    for _, params in fake.cursor_obj.calls[1:]:
        assert params == (30,)


def test_usage_summary_store_returns_error_payload_on_db_failure():
    def broken_connect(_, **kwargs):
        raise psycopg.OperationalError("connection refused")

    store = UsageSummaryStore("postgresql://example", connect=broken_connect)
    summary = store.summary(7)

    assert summary["enabled"] is False
    assert summary["period_days"] == 7
    assert "database unavailable" in summary["error"]
    assert summary["totals"] == {}
    assert summary["top_models"] == []


def test_usage_summary_store_passes_connect_timeout():
    received_kwargs = {}

    def connect(_, **kwargs):
        received_kwargs.update(kwargs)
        raise psycopg.OperationalError("stop")

    store = UsageSummaryStore("postgresql://example", connect=connect, db_timeout_seconds=3)
    store.summary(1)

    assert received_kwargs.get("connect_timeout") == 3


@pytest.mark.asyncio
async def test_dashboard_html_route_returns_page():
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, database_url=None)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/dashboard")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "AI Gateway" in response.text
    assert "/dashboard/api/live" in response.text
    assert "/dashboard/api/usage" in response.text


@pytest.mark.asyncio
async def test_dashboard_live_api_returns_json_shape():
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, database_url=None)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/dashboard/api/live")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"health", "readiness", "metrics", "redis", "config"}
    assert payload["config"]["default_model"] == "coder"
    assert "enabled" in payload["redis"]


@pytest.mark.asyncio
async def test_dashboard_usage_api_defaults_to_30_days_when_ledger_disabled():
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, database_url=None)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/dashboard/api/usage?days=999")

    assert response.status_code == 200
    assert response.json() == {
        "enabled": False,
        "period_days": 30,
        "totals": {},
        "top_models": [],
        "daily_usage": [],
        "top_keys": [],
        "recent_failures": [],
    }


@pytest.mark.asyncio
async def test_dashboard_routes_do_not_break_chat_or_metrics():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            },
        )

    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        database_url=None,
        transport=httpx.MockTransport(handler),
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        chat = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "dash-regression"},
            json={"model": "coder", "messages": [{"role": "user", "content": "say OK"}]},
        )
        metrics = await client.get("/metrics")
        live = await client.get("/dashboard/api/live")

    assert chat.status_code == 200
    assert metrics.status_code == 200
    assert live.status_code == 200
    assert metrics.json()["requests_total"] == 1
    assert live.json()["metrics"]["requests_total"] == 1

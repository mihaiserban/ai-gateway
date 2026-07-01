import json

import httpx
import pytest

from router.main import create_app
from router.metrics import Metrics

ENV_OLLAMA = {"OLLAMA_API_BASE": "http://ollama", "OLLAMA_API_KEY": "x"}
ENV_GO = {"OPENCODE_GO_API_BASE": "http://go", "OPENCODE_GO_API_KEY": "x"}
ENV_DEEPSEEK = {"DEEPSEEK_API_KEY": "x"}


def _env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)


@pytest.mark.asyncio
async def test_metrics_counts_requests_and_models(monkeypatch, simple_route_config_path: str):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    _env(monkeypatch, {**ENV_OLLAMA, **ENV_GO})
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=httpx.MockTransport(handler),
        config_path=simple_route_config_path,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/v1/chat/completions",
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "a"}]},
        )
        _env(monkeypatch, {**ENV_OLLAMA, **ENV_DEEPSEEK})
        await client.post(
            "/v1/chat/completions",
            json={"model": "coder", "messages": [{"role": "user", "content": "b"}]},
        )

    metrics: Metrics = app.state.metrics
    assert metrics.requests_total == 2
    assert metrics.fallback_count_total == 0


@pytest.mark.asyncio
async def test_metrics_counts_fallbacks(monkeypatch, simple_route_config_path: str):
    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        if model == "ollama-cloud.kimi-k2.7-code":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    _env(monkeypatch, {**ENV_OLLAMA, **ENV_GO})
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=httpx.MockTransport(handler),
        config_path=simple_route_config_path,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    assert response.headers["X-Gateway-Fallback-Count"] == "1"

    metrics: Metrics = app.state.metrics
    assert metrics.requests_total == 1
    assert metrics.fallback_count_total == 1
    assert metrics.served_model_counts == {"opencode-go.kimi-k2.7-code": 1}


@pytest.mark.asyncio
async def test_metrics_counts_cache_hit_miss_and_unknown(monkeypatch, simple_route_config_path: str):
    cache_headers = iter(
        [
            {"x-litellm-cache-hit": "true"},
            {"x-litellm-cache-hit": "false"},
            {},
        ]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]}, headers=next(cache_headers))

    _env(monkeypatch, {**ENV_OLLAMA, **ENV_GO})
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=httpx.MockTransport(handler),
        config_path=simple_route_config_path,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for session_id in ("hit", "miss", "unknown"):
            await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test", "X-Session-Id": session_id},
                json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "say hello"}]},
            )

    payload = app.state.metrics.snapshot()
    assert payload["cache_counts"] == {"hit": 1, "miss": 1, "unknown": 1}


@pytest.mark.asyncio
async def test_metrics_counts_cache_key_without_hit_header_as_miss(monkeypatch, simple_route_config_path: str):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "OK"}}]},
            headers={"x-litellm-cache-key": "cache-key"},
        )

    _env(monkeypatch, {**ENV_OLLAMA, **ENV_GO})
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=httpx.MockTransport(handler),
        config_path=simple_route_config_path,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "cache-key-miss"},
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "say hello"}]},
        )

    payload = app.state.metrics.snapshot()
    assert payload["cache_counts"] == {"hit": 0, "miss": 1, "unknown": 0}


@pytest.mark.asyncio
async def test_metrics_tracks_availability_for_each_upstream_attempt(monkeypatch, simple_route_config_path: str):
    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        if model == "ollama-cloud.kimi-k2.7-code":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    _env(monkeypatch, {**ENV_OLLAMA, **ENV_GO})
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=httpx.MockTransport(handler),
        config_path=simple_route_config_path,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "availability"},
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}]},
        )

    provider_availability = app.state.metrics.snapshot()["provider_availability"]
    assert provider_availability["ollama-cloud.kimi-k2.7-code"]["attempts"] == 1
    assert provider_availability["ollama-cloud.kimi-k2.7-code"]["failures"] == 1
    assert provider_availability["ollama-cloud.kimi-k2.7-code"]["retryable_failures"] == 1
    assert provider_availability["opencode-go.kimi-k2.7-code"]["attempts"] == 1
    assert provider_availability["opencode-go.kimi-k2.7-code"]["successes"] == 1


@pytest.mark.asyncio
async def test_metrics_endpoint_returns_counts(monkeypatch, simple_route_config_path: str):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    _env(monkeypatch, {**ENV_OLLAMA, **ENV_GO})
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=httpx.MockTransport(handler),
        config_path=simple_route_config_path,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "m1"},
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}]},
        )
        resp = await client.get("/metrics")

    assert resp.status_code == 200
    payload = resp.json()
    assert set(payload.keys()) == {
        "requests_total",
        "fallback_count_total",
        "selected_model_counts",
        "served_model_counts",
        "cache_counts",
        "provider_availability",
        "routing_state",
    }
    assert payload["requests_total"] == 1

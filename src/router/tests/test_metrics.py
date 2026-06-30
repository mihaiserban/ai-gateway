import json

import httpx
import pytest

from router.main import create_app
from router.metrics import Metrics


@pytest.mark.asyncio
async def test_metrics_counts_requests_and_models():
    """Two requests using different classified models bump the right counters."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # opencodego-fast: code signal ("refactor ... .py")
        await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "s1"},
            json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )
        # fast: short plain prompt with no code/reasoning signals
        await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "s2"},
            json={"messages": [{"role": "user", "content": "say hello"}]},
        )

    metrics: Metrics = app.state.metrics
    assert metrics.requests_total == 2
    assert metrics.selected_model_counts == {"opencodego-fast": 1, "fast": 1}
    assert metrics.fallback_count_total == 0


@pytest.mark.asyncio
async def test_metrics_counts_fallbacks():
    """One request that falls back adds fallback_count_total and records the final model."""

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        if model == "opencodego-fast":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "fb"},
            json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 200
    assert response.headers["X-Gateway-Fallback-Count"] == "1"

    metrics: Metrics = app.state.metrics
    assert metrics.requests_total == 1
    assert metrics.fallback_count_total == 1
    assert metrics.selected_model_counts == {"fast": 1}


@pytest.mark.asyncio
async def test_metrics_endpoint_returns_counts():
    """GET /metrics returns the documented JSON shape."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "m1"},
            json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )
        resp = await client.get("/metrics")

    assert resp.status_code == 200
    payload = resp.json()
    assert set(payload.keys()) == {"requests_total", "fallback_count_total", "selected_model_counts"}
    assert payload["requests_total"] == 1
    assert payload["fallback_count_total"] == 0
    assert payload["selected_model_counts"] == {"opencodego-fast": 1}
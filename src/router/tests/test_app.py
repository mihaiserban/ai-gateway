import json

import httpx
import pytest

from router.main import create_app


@pytest.mark.asyncio
async def test_healthz_reports_router_ok():
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["router"] == "ok"


@pytest.mark.asyncio
async def test_models_proxies_to_litellm():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"id": "fast"}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/models", headers={"Authorization": "Bearer test"})

    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "fast"
    assert seen["url"] == "http://litellm:4000/v1/models"


@pytest.mark.asyncio
async def test_chat_rewrites_model_to_classified_alias():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "abc"},
            json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 200
    assert seen["json"]["model"] == "opencodego-fast"
    assert response.headers["X-Gateway-Model"] == "opencodego-fast"


@pytest.mark.asyncio
async def test_chat_keeps_warm_session_model():
    seen_models = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_models.append(json.loads(request.content)["model"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "sticky"},
            json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )
        await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "sticky"},
            json={"messages": [{"role": "user", "content": "say hello"}]},
        )

    assert seen_models == ["opencodego-fast", "opencodego-fast"]


@pytest.mark.asyncio
async def test_unsupported_v1_path_returns_501():
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/responses", json={})

    assert response.status_code == 501

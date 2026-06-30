import json

import httpx
import pytest

from router.main import create_app

SSE_BODY = b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\ndata: {"choices":[{"delta":{"content":" world"}}]}\n\ndata: [DONE]\n\n'


@pytest.mark.asyncio
async def test_chat_stream_passthrough_forwards_chunks(simple_route_config_path: str):
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=SSE_BODY,
            headers={"content-type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport, config_path=simple_route_config_path)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "stream-1"},
            json={
                "model": "coder",
                "messages": [{"role": "user", "content": "please refactor src/app.py"}],
                "stream": True,
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    # The upstream model alias was rewritten to the explicit alias.
    assert seen["json"]["model"] == "coder"
    # All SSE chunks arrive in order, byte-for-byte.
    assert response.content == SSE_BODY


@pytest.mark.asyncio
async def test_chat_stream_sets_gateway_headers(simple_route_config_path: str):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=SSE_BODY,
            headers={"content-type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport, config_path=simple_route_config_path)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "stream-headers"},
            json={
                "model": "coder",
                "messages": [{"role": "user", "content": "please refactor src/app.py"}],
                "stream": True,
            },
        )

    assert response.status_code == 200
    assert response.headers["X-Gateway-Model"] == "coder"
    assert response.headers["X-Gateway-Reason"] == "explicit-model"
    assert response.headers["X-Gateway-Fallback-Count"] == "0"


@pytest.mark.asyncio
async def test_chat_stream_writes_session_after_200(simple_route_config_path: str):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=SSE_BODY,
            headers={"content-type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport, config_path=simple_route_config_path)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "stream-session"},
            json={
                "model": "coder",
                "messages": [{"role": "user", "content": "please refactor src/app.py"}],
                "stream": True,
            },
        )

    session = await app.state.session_store.get("stream-session")
    assert session is not None
    assert session["model"] == "coder"
    assert session["reason"] == "explicit-model"


@pytest.mark.asyncio
async def test_chat_stream_fallback_before_stream_starts(simple_route_config_path: str):
    seen_models: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        if model == "coder":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(
            200,
            content=SSE_BODY,
            headers={"content-type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport, config_path=simple_route_config_path)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "stream-fallback"},
            json={
                "model": "coder",
                "messages": [{"role": "user", "content": "please refactor src/app.py"}],
                "stream": True,
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    assert seen_models == ["coder", "explorer"]
    assert response.headers["X-Gateway-Model"] == "explorer"
    assert response.headers["X-Gateway-Fallback-From"] == "coder"
    assert response.headers["X-Gateway-Fallback-Count"] == "1"
    assert response.content == SSE_BODY

import json

import httpx
import pytest

from router.main import create_app
from router.usage_events import UsageEvent

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
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=transport,
        config_path=simple_route_config_path,
    )

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
    assert seen["json"]["stream_options"] == {"include_usage": True}
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
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=transport,
        config_path=simple_route_config_path,
    )

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
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=transport,
        config_path=simple_route_config_path,
    )

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
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=transport,
        config_path=simple_route_config_path,
    )

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


SSE_WITH_USAGE = b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\ndata: {"choices":[{"delta":{"content":" world"}}],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\ndata: [DONE]\n\n'


class FakeSink:
    def __init__(self) -> None:
        self.events: list[UsageEvent] = []

    async def record(self, event: UsageEvent) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_chat_stream_extracts_usage_from_sse(simple_route_config_path: str):
    sink = FakeSink()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=SSE_WITH_USAGE,
            headers={"content-type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=transport,
        config_path=simple_route_config_path,
        usage_sink=sink,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "stream-usage"},
            json={
                "model": "coder",
                "messages": [{"role": "user", "content": "say hello"}],
                "stream": True,
            },
        )

    assert response.status_code == 200
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.prompt_tokens == 10
    assert event.completion_tokens == 5
    assert event.total_tokens == 15
    assert event.stream is True
    assert event.served_model == "coder"


@pytest.mark.asyncio
async def test_chat_stream_no_usage_in_sse_records_none(simple_route_config_path: str):
    sink = FakeSink()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=SSE_BODY,
            headers={"content-type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=transport,
        config_path=simple_route_config_path,
        usage_sink=sink,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "stream-no-usage"},
            json={
                "model": "coder",
                "messages": [{"role": "user", "content": "say hello"}],
                "stream": True,
            },
        )

    assert response.status_code == 200
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.prompt_tokens is None
    assert event.completion_tokens is None
    assert event.total_tokens is None
    assert event.stream is True


@pytest.mark.asyncio
async def test_chat_stream_aborted_still_emits_usage_event(simple_route_config_path: str):
    sink = FakeSink()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=SSE_WITH_USAGE,
            headers={"content-type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=transport,
        config_path=simple_route_config_path,
        usage_sink=sink,
    )

    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        client.stream(
            "POST",
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "stream-abort"},
            json={
                "model": "coder",
                "messages": [{"role": "user", "content": "say hello"}],
                "stream": True,
            },
        ) as response,
    ):
        assert response.status_code == 200
        # Read only the first byte then abort
        try:
            async for _chunk in response.aiter_bytes(1):
                break
        except Exception:
            pass

    # Usage event should still be emitted because the body iterator's finally
    # block runs and calls on_close with whatever was buffered.
    assert len(sink.events) == 1
    assert sink.events[0].stream is True

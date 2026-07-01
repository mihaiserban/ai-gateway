import json

import httpx
import pytest

from router.main import create_app
from router.usage_events import UsageEvent

SSE_BODY = b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\ndata: {"choices":[{"delta":{"content":" world"}}]}\n\ndata: [DONE]\n\n'

ENV_OLLAMA = {"OLLAMA_API_BASE": "http://ollama", "OLLAMA_API_KEY": "x"}
ENV_GO = {"OPENCODE_GO_API_BASE": "http://go", "OPENCODE_GO_API_KEY": "x"}


def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {**ENV_OLLAMA, **ENV_GO}.items():
        monkeypatch.setenv(key, value)


def _app(monkeypatch, simple_route_config_path: str, transport: httpx.AsyncBaseTransport):
    _env(monkeypatch)
    return create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=transport,
        config_path=simple_route_config_path,
    )


@pytest.mark.asyncio
async def test_chat_stream_passthrough_forwards_chunks(monkeypatch, simple_route_config_path: str):
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, content=SSE_BODY, headers={"content-type": "text/event-stream"})

    app = _app(monkeypatch, simple_route_config_path, httpx.MockTransport(handler))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    assert seen["json"]["model"] == "ollama-cloud.kimi-k2.7-code"
    assert seen["json"]["stream_options"] == {"include_usage": True}
    assert response.content == SSE_BODY


@pytest.mark.asyncio
async def test_chat_stream_sets_gateway_headers(monkeypatch, simple_route_config_path: str):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=SSE_BODY, headers={"content-type": "text/event-stream"})

    app = _app(monkeypatch, simple_route_config_path, httpx.MockTransport(handler))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        )

    assert response.status_code == 200
    assert response.headers["X-Gateway-Model-Kind"] == "registry-model"
    assert response.headers["X-Gateway-Served-Deployment"] == "ollama-cloud.kimi-k2.7-code"
    assert response.headers["X-Gateway-Fallback-Count"] == "0"


@pytest.mark.asyncio
async def test_chat_stream_writes_session_after_200(monkeypatch, simple_route_config_path: str):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=SSE_BODY, headers={"content-type": "text/event-stream"})

    app = _app(monkeypatch, simple_route_config_path, httpx.MockTransport(handler))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "stream-session"},
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        )

    import hashlib

    key = hashlib.sha256(hashlib.sha256(b"Bearer test").hexdigest().encode() + b":stream-session").hexdigest()
    session = await app.state.session_store.get(key)
    assert session is not None
    assert session["served_deployment"] == "ollama-cloud.kimi-k2.7-code"
    assert session["model_kind"] == "registry-model"


@pytest.mark.asyncio
async def test_chat_stream_fallback_before_stream_starts(monkeypatch, simple_route_config_path: str):
    seen_models: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        if model == "ollama-cloud.kimi-k2.7-code":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, content=SSE_BODY, headers={"content-type": "text/event-stream"})

    app = _app(monkeypatch, simple_route_config_path, httpx.MockTransport(handler))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    assert seen_models == ["ollama-cloud.kimi-k2.7-code", "opencode-go.kimi-k2.7-code"]
    assert response.headers["X-Gateway-Served-Deployment"] == "opencode-go.kimi-k2.7-code"
    assert response.headers["X-Gateway-Fallback-Count"] == "1"
    assert response.content == SSE_BODY


SSE_WITH_USAGE = (
    b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
    b'data: {"choices":[{"delta":{"content":" world"}}],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n'
    b"data: [DONE]\n\n"
)


class FakeSink:
    def __init__(self) -> None:
        self.events: list[UsageEvent] = []

    async def record(self, event: UsageEvent) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_chat_stream_extracts_usage_from_sse(monkeypatch, simple_route_config_path: str):
    sink = FakeSink()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=SSE_WITH_USAGE, headers={"content-type": "text/event-stream"})

    app = _app(monkeypatch, simple_route_config_path, httpx.MockTransport(handler))
    app.state.usage_sink = sink

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "stream-usage"},
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        )

    assert response.status_code == 200
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.prompt_tokens == 10
    assert event.completion_tokens == 5
    assert event.total_tokens == 15
    assert event.stream is True
    assert event.served_model == "ollama-cloud.kimi-k2.7-code"


@pytest.mark.asyncio
async def test_chat_stream_no_usage_in_sse_records_none(monkeypatch, simple_route_config_path: str):
    sink = FakeSink()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=SSE_BODY, headers={"content-type": "text/event-stream"})

    app = _app(monkeypatch, simple_route_config_path, httpx.MockTransport(handler))
    app.state.usage_sink = sink

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "stream-no-usage"},
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        )

    assert response.status_code == 200
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.prompt_tokens is None
    assert event.completion_tokens is None
    assert event.stream is True


@pytest.mark.asyncio
async def test_chat_stream_aborted_still_emits_usage_event(monkeypatch, simple_route_config_path: str):
    sink = FakeSink()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=SSE_WITH_USAGE, headers={"content-type": "text/event-stream"})

    app = _app(monkeypatch, simple_route_config_path, httpx.MockTransport(handler))
    app.state.usage_sink = sink

    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        client.stream(
            "POST",
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "stream-abort"},
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        ) as response,
    ):
        assert response.status_code == 200
        try:
            async for _chunk in response.aiter_bytes(1):
                break
        except Exception:
            pass

    assert len(sink.events) == 1
    assert sink.events[0].stream is True

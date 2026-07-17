import asyncio
import json

import httpx
import pytest

from router.main import _streaming_response, create_app
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


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


class BlockingStream(httpx.AsyncByteStream):
    def __init__(self, started: asyncio.Event, release: asyncio.Event) -> None:
        self.started = started
        self.release = release

    async def __aiter__(self):
        self.started.set()
        await self.release.wait()
        yield SSE_BODY


class TrackingSemaphore(asyncio.Semaphore):
    def __init__(self) -> None:
        super().__init__(1)
        self.acquire_calls = 0
        self.second_acquire_attempted = asyncio.Event()

    async def acquire(self) -> bool:
        self.acquire_calls += 1
        if self.acquire_calls == 2:
            self.second_acquire_attempted.set()
        return await super().acquire()


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
async def test_chat_stream_extracts_usage_before_final_chunks(monkeypatch, simple_route_config_path: str):
    sink = FakeSink()
    chunks = [
        b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":" world"}}],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n',
        b'data: {"choices":[{"delta":{"content":"."}}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=ChunkStream(chunks), headers={"content-type": "text/event-stream"})

    app = _app(monkeypatch, simple_route_config_path, httpx.MockTransport(handler))
    app.state.usage_sink = sink

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "stream-usage"},
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        )

    assert response.status_code == 200
    event = sink.events[0]
    assert event.prompt_tokens == 10
    assert event.completion_tokens == 5
    assert event.total_tokens == 15


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
async def test_aborted_stream_closes_upstream_and_runs_callback():
    callback_payloads = []

    async def on_close(payload):
        callback_payloads.append(payload)

    upstream = httpx.Response(
        200,
        stream=ChunkStream([SSE_WITH_USAGE]),
        headers={"content-type": "text/event-stream"},
    )
    response = _streaming_response(upstream, on_close=on_close)

    assert await anext(response.body_iterator) == SSE_WITH_USAGE
    await response.body_iterator.aclose()

    assert upstream.is_closed is True
    assert callback_payloads == [{"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}]


@pytest.mark.asyncio
async def test_upstream_concurrency_limit_covers_full_stream_lifetime(monkeypatch, simple_route_config_path: str):
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        started = first_started if calls == 1 else second_started
        return httpx.Response(
            200,
            stream=BlockingStream(started, release),
            headers={"content-type": "text/event-stream"},
        )

    app = _app(monkeypatch, simple_route_config_path, httpx.MockTransport(handler))
    semaphore = TrackingSemaphore()
    app.state.upstream_semaphore = semaphore
    payload = {"model": "kimi-k2.7-code", "messages": [], "stream": True}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = asyncio.create_task(client.post("/v1/chat/completions", json=payload))
        await asyncio.wait_for(first_started.wait(), timeout=1)
        second = asyncio.create_task(client.post("/v1/chat/completions", json=payload))
        await asyncio.wait_for(semaphore.second_acquire_attempted.wait(), timeout=1)
        assert second_started.is_set() is False
        assert calls == 1
        release.set()
        responses = await asyncio.gather(first, second)

    assert [response.status_code for response in responses] == [200, 200]


@pytest.mark.asyncio
async def test_cancelled_session_write_closes_stream_and_releases_concurrency_slot(
    monkeypatch, simple_route_config_path: str
):
    session_write_started = asyncio.Event()
    upstream_response = None

    class BlockingSessionStore:
        async def get(self, session_id):
            return None

        async def set(self, session_id, value, ttl_seconds):
            session_write_started.set()
            await asyncio.Event().wait()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_response
        upstream_response = httpx.Response(
            200,
            stream=ChunkStream([SSE_BODY]),
            headers={"content-type": "text/event-stream"},
        )
        return upstream_response

    app = _app(monkeypatch, simple_route_config_path, httpx.MockTransport(handler))
    semaphore = asyncio.Semaphore(1)
    app.state.upstream_semaphore = semaphore
    app.state.session_store = BlockingSessionStore()
    payload = {"model": "kimi-k2.7-code", "messages": [], "stream": True}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        request = asyncio.create_task(
            client.post("/v1/chat/completions", headers={"X-Session-Id": "cancelled"}, json=payload)
        )
        await asyncio.wait_for(session_write_started.wait(), timeout=1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

    assert upstream_response is not None
    assert upstream_response.is_closed is True
    assert semaphore.locked() is False

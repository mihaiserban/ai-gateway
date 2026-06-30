import json
import logging

import httpx
import pytest

from router.main import create_app


def _log_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "router"]


@pytest.mark.asyncio
async def test_chat_logs_request_metadata(caplog):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    with caplog.at_level(logging.INFO, logger="router"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test", "X-Session-Id": "log-meta"},
                json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
            )

    assert response.status_code == 200
    lines = _log_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert "session_id_hash=" in line
    assert "model=opencodego-fast" in line
    assert "reason=classified" in line
    assert "status=200" in line
    assert "latency_ms=" in line
    assert "fallback_count=0" in line
    # fallback_from omitted when fallback_count == 0
    assert "fallback_from=" not in line


@pytest.mark.asyncio
async def test_chat_logs_fallback_from_when_fallback_occurred(caplog):
    seen_models: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        if model == "opencodego-fast":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    with caplog.at_level(logging.INFO, logger="router"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test", "X-Session-Id": "log-fb"},
                json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
            )

    assert response.status_code == 200
    lines = _log_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert "fallback_count=1" in line
    assert "fallback_from=opencodego-fast" in line


@pytest.mark.asyncio
async def test_chat_log_omits_raw_token(caplog):
    secret = "sk-super-secret-bearer-token-xyz"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    with caplog.at_level(logging.INFO, logger="router"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {secret}", "X-Session-Id": "log-secret"},
                json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
            )

    lines = _log_lines(caplog)
    assert len(lines) == 1
    assert secret not in lines[0]
    assert "Bearer" not in lines[0]


@pytest.mark.asyncio
async def test_chat_log_omits_prompt_content(caplog):
    user_content = "my-unique-sensitive-prompt-content-12345"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    with caplog.at_level(logging.INFO, logger="router"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test", "X-Session-Id": "log-prompt"},
                json={"messages": [{"role": "user", "content": user_content}]},
            )

    lines = _log_lines(caplog)
    assert len(lines) == 1
    assert user_content not in lines[0]


@pytest.mark.asyncio
async def test_chat_log_on_error_path_logs_error_status(caplog):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("request timed out")

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    with caplog.at_level(logging.INFO, logger="router"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test", "X-Session-Id": "log-err"},
                json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
            )

    assert response.status_code == 504
    lines = _log_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert "status=504" in line
    assert "model=" in line
    assert "reason=" in line
    assert "latency_ms=" in line
    assert "fallback_count=" in line


@pytest.mark.asyncio
async def test_chat_log_on_upstream_error_status_logs_status(caplog):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    with caplog.at_level(logging.INFO, logger="router"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test", "X-Session-Id": "log-up-err"},
                json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
            )

    assert response.status_code == 503
    lines = _log_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert "status=503" in line


@pytest.mark.asyncio
async def test_chat_stream_logs_request_metadata(caplog):
    sse_body = b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\ndata: [DONE]\n\n'

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse_body,
            headers={"content-type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    with caplog.at_level(logging.INFO, logger="router"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test", "X-Session-Id": "log-stream"},
                json={
                    "messages": [{"role": "user", "content": "please refactor src/app.py"}],
                    "stream": True,
                },
            )

    assert response.status_code == 200
    lines = _log_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert "model=opencodego-fast" in line
    assert "status=200" in line
    assert "latency_ms=" in line
    assert "fallback_count=0" in line

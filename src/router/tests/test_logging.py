import json
import logging

import httpx
import pytest

from router.main import create_app


def _log_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "router"]


@pytest.mark.asyncio
async def test_chat_logs_request_metadata(caplog, simple_route_config_path: str):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport, config_path=simple_route_config_path)

    with caplog.at_level(logging.INFO, logger="router"):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test", "X-Session-Id": "log-meta"},
                json={"model": "coder", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
            )

    assert response.status_code == 200
    lines = _log_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert "session_id_hash=" in line
    assert "model=coder" in line
    expected_provider_model = app.state.route_config.provider_models["coder"]
    assert f"provider_model={expected_provider_model}" in line
    assert "reason=explicit-model" in line
    assert "status=200" in line
    assert "latency_ms=" in line
    assert "fallback_count=0" in line
    # fallback_from omitted when fallback_count == 0
    assert "fallback_from=" not in line


@pytest.mark.asyncio
async def test_chat_logs_fallback_from_when_fallback_occurred(caplog, simple_route_config_path: str):
    seen_models: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        if model == "coder":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport, config_path=simple_route_config_path)

    with caplog.at_level(logging.INFO, logger="router"):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test", "X-Session-Id": "log-fb"},
                json={"model": "coder", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
            )

    assert response.status_code == 200
    lines = _log_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert "fallback_count=1" in line
    assert "fallback_from=coder" in line
    fallback_model = app.state.route_config.fallbacks["coder"][0]
    assert f"model={fallback_model}" in line
    expected_provider_model = app.state.route_config.provider_models[fallback_model]
    assert f"provider_model={expected_provider_model}" in line


@pytest.mark.asyncio
async def test_chat_log_omits_raw_token(caplog, simple_route_config_path: str):
    secret = "sk-super-secret-bearer-token-xyz"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport, config_path=simple_route_config_path)

    with caplog.at_level(logging.INFO, logger="router"):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {secret}", "X-Session-Id": "log-secret"},
                json={"model": "coder", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
            )

    lines = _log_lines(caplog)
    assert len(lines) == 1
    assert secret not in lines[0]
    assert "Bearer" not in lines[0]


@pytest.mark.asyncio
async def test_chat_log_omits_prompt_content(caplog, simple_route_config_path: str):
    user_content = "my-unique-sensitive-prompt-content-12345"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport, config_path=simple_route_config_path)

    with caplog.at_level(logging.INFO, logger="router"):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test", "X-Session-Id": "log-prompt"},
                json={"messages": [{"role": "user", "content": user_content}]},
            )

    lines = _log_lines(caplog)
    assert len(lines) == 1
    assert user_content not in lines[0]


@pytest.mark.asyncio
async def test_chat_log_on_error_path_logs_error_status(caplog, simple_route_config_path: str):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("request timed out")

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport, config_path=simple_route_config_path)

    with caplog.at_level(logging.INFO, logger="router"):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test", "X-Session-Id": "log-err"},
                json={"model": "coder", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
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
async def test_chat_log_on_upstream_error_status_logs_status(caplog, simple_route_config_path: str):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport, config_path=simple_route_config_path)

    with caplog.at_level(logging.INFO, logger="router"):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test", "X-Session-Id": "log-up-err"},
                json={"model": "coder", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
            )

    assert response.status_code == 503
    lines = _log_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert "status=503" in line


@pytest.mark.asyncio
async def test_chat_stream_logs_request_metadata(caplog, simple_route_config_path: str):
    sse_body = b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\ndata: [DONE]\n\n'

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse_body,
            headers={"content-type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport, config_path=simple_route_config_path)

    with caplog.at_level(logging.INFO, logger="router"):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test", "X-Session-Id": "log-stream"},
                json={
                    "model": "coder",
                    "messages": [{"role": "user", "content": "please refactor src/app.py"}],
                    "stream": True,
                },
            )

    assert response.status_code == 200
    lines = _log_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert "model=coder" in line
    assert "status=200" in line
    assert "latency_ms=" in line
    assert "fallback_count=0" in line

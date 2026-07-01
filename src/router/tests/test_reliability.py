from __future__ import annotations

import json

import httpx
import pytest

from router.main import create_app
from router.routing import DEFAULT_TIMEOUT_SECONDS, RouteConfig, is_retryable_failure

ENV_OLLAMA = {"OLLAMA_API_BASE": "http://ollama", "OLLAMA_API_KEY": "x"}
ENV_GO = {"OPENCODE_GO_API_BASE": "http://go", "OPENCODE_GO_API_KEY": "x"}
ENV_DEEPSEEK = {"DEEPSEEK_API_KEY": "x"}
ENV_ALL = {**ENV_OLLAMA, **ENV_GO, **ENV_DEEPSEEK}


def _env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def _recorder():
    calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        calls.append(delay)

    return fake_sleep, calls


def test_route_config_defaults_for_backoff():
    config = RouteConfig()
    assert config.retry_base_delay == 0.2
    assert config.retry_max_delay == 2.0


def test_route_config_loads_backoff_params(tmp_path):
    from router import config as config_mod

    cfg_path = tmp_path / "router_config.yaml"
    cfg_path.write_text(
        """
cache_ttl_seconds: 600
default_model: coder
retry_base_delay: 0.5
retry_max_delay: 5.0
""",
    )
    config = config_mod.load_route_config(config_path=str(cfg_path))
    assert config.retry_base_delay == 0.5
    assert config.retry_max_delay == 5.0


def test_default_timeout_is_120():
    assert DEFAULT_TIMEOUT_SECONDS == 120


def test_is_retryable_classifies_int_and_str():
    assert is_retryable_failure(503)
    assert is_retryable_failure("transport_error")
    assert not is_retryable_failure(400)
    assert not is_retryable_failure(401)


@pytest.mark.asyncio
async def test_backoff_sleeps_between_fallback_attempts(monkeypatch, simple_route_config_path: str):
    sleep, calls = _recorder()
    _env(monkeypatch, {**ENV_OLLAMA, **ENV_GO})
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=httpx.MockTransport(_retry_handler()),
        config_path=simple_route_config_path,
    )
    app.state.async_sleep = sleep

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    assert calls == [0.2]


def _retry_handler():
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen.append(model)
        if model == "ollama-cloud.kimi-k2.7-code":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    return handler


@pytest.mark.asyncio
async def test_backoff_no_sleep_on_non_retryable_400(monkeypatch, simple_route_config_path: str):
    sleep, calls = _recorder()
    _env(monkeypatch, {**ENV_OLLAMA, **ENV_GO})

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad request"})

    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=httpx.MockTransport(handler),
        config_path=simple_route_config_path,
    )
    app.state.async_sleep = sleep

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 400
    assert calls == []


@pytest.mark.asyncio
async def test_backoff_grows_exponentially(monkeypatch, simple_route_config_path: str):
    sleep, calls = _recorder()
    _env(monkeypatch, ENV_ALL)
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen.append(model)
        if model in {"ollama-cloud.kimi-k2.7-code", "deepseek-api.deepseek-v4-pro"}:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=httpx.MockTransport(handler),
        config_path=simple_route_config_path,
    )
    app.state.async_sleep = sleep

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "coder", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    assert calls == [0.2, 0.4]


@pytest.mark.asyncio
async def test_backoff_capped_at_max_delay(monkeypatch, simple_route_config_path: str):
    sleep, calls = _recorder()
    _env(monkeypatch, ENV_ALL)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=httpx.MockTransport(handler),
        config_path=simple_route_config_path,
    )
    app.state.async_sleep = sleep

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/v1/chat/completions",
            json={"model": "coder", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert calls == [0.2, 0.4]


@pytest.mark.asyncio
async def test_backoff_no_sleep_after_final_attempt(monkeypatch, simple_route_config_path: str):
    sleep, calls = _recorder()
    _env(monkeypatch, ENV_ALL)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=httpx.MockTransport(handler),
        config_path=simple_route_config_path,
    )
    app.state.async_sleep = sleep

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/v1/chat/completions",
            json={"model": "coder", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert len(calls) == 2

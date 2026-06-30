from __future__ import annotations

import json

import httpx
import pytest

from router.main import create_app
from router.routing import RouteConfig, _timeout_for

# ---------------------------------------------------------------------------
# Per-alias timeout helper
# ---------------------------------------------------------------------------


def test_timeout_for_returns_configured_value():
    config = RouteConfig(timeouts={"deepseek-pro": 5})
    assert _timeout_for(config, "deepseek-pro") == 5


def test_timeout_for_falls_back_to_default_120():
    config = RouteConfig(timeouts={"deepseek-pro": 5})
    assert _timeout_for(config, "unknown-alias") == 120


def test_timeout_for_default_empty_config():
    config = RouteConfig()
    assert _timeout_for(config, "anything") == 120


@pytest.mark.asyncio
async def test_per_alias_timeout_used_for_proxy(monkeypatch, tmp_path):
    """The httpx client created for a chat request must use the alias's timeout."""
    captured: list[httpx.Timeout] = []
    real_async_client = httpx.AsyncClient

    class CapturingAsyncClient(real_async_client):
        def __init__(self, *args, **kwargs):
            timeout = kwargs.get("timeout", httpx.USE_CLIENT_DEFAULT)
            if timeout is not httpx.USE_CLIENT_DEFAULT and not isinstance(timeout, httpx.Timeout):
                timeout = httpx.Timeout(timeout)
            captured.append(timeout)
            super().__init__(*args, **kwargs)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)

    cfg_path = tmp_path / "test-alias-timeout-config.yaml"
    cfg_path.write_text(
        """
cache_ttl_seconds: 600
allowed_models:
  - fast
  - deepseek-pro
  - opencodego-fast
  - opencodego-code
  - ollama-cloud
fallbacks:
  fast: []
  deepseek-pro: []
  opencodego-fast: []
  opencodego-code: []
  ollama-cloud: []
timeouts:
  opencodego-fast: 5
""",
    )

    monkeypatch.setattr(httpx, "AsyncClient", CapturingAsyncClient)
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=transport,
        config_path=str(cfg_path),
        litellm_config_path="/tmp/missing-litellm.yaml",
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "timeout-proxy"},
            json={"model": "opencodego-fast", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 200
    # The inner client created by _proxy_json must have a 5s connect/read timeout.
    assert captured, "no inner httpx.AsyncClient was constructed"
    timeout = captured[-1]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 5
    assert timeout.read == 5


# ---------------------------------------------------------------------------
# Backoff between fallback attempts
# ---------------------------------------------------------------------------


def _recorder():
    calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        calls.append(delay)

    return fake_sleep, calls


@pytest.mark.asyncio
async def test_backoff_sleeps_between_fallback_attempts():
    sleep, calls = _recorder()
    seen_models: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        if model == "opencodego-fast":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)
    app.state.async_sleep = sleep

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "backoff-1"},
            json={"model": "opencodego-fast", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 200
    assert seen_models == ["opencodego-fast", "fast"]
    assert calls == [0.2]


@pytest.mark.asyncio
async def test_backoff_not_called_on_client_error():
    sleep, calls = _recorder()
    seen_models: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_models.append(json.loads(request.content)["model"])
        return httpx.Response(400, json={"error": "bad request"})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)
    app.state.async_sleep = sleep

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "no-sleep-400"},
            json={"model": "opencodego-fast", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 400
    assert seen_models == ["opencodego-fast"]
    assert calls == []


@pytest.mark.asyncio
async def test_backoff_grows_exponentially():
    sleep, calls = _recorder()
    seen_models: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        if model in {"opencodego-fast", "fast"}:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)
    app.state.async_sleep = sleep

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "backoff-grow"},
            json={"model": "opencodego-fast", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 200
    assert seen_models == ["opencodego-fast", "fast", "deepseek-pro"]
    assert calls == [0.2, 0.4]


@pytest.mark.asyncio
async def test_backoff_capped_at_max_delay():
    sleep, calls = _recorder()
    seen_models: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        return httpx.Response(503, json={"error": "unavailable"})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)
    app.state.async_sleep = sleep

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "backoff-cap"},
            json={"model": "opencodego-fast", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 503
    assert seen_models == ["opencodego-fast", "fast", "deepseek-pro"]
    assert calls == [0.2, 0.4]


@pytest.mark.asyncio
async def test_backoff_no_sleep_after_final_attempt():
    sleep, calls = _recorder()
    seen_models: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        return httpx.Response(503, json={"error": "unavailable"})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)
    app.state.async_sleep = sleep

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "final-attempt"},
            json={"model": "opencodego-fast", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert seen_models == ["opencodego-fast", "fast", "deepseek-pro"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_backoff_no_sleep_on_non_retryable_404():
    sleep, calls = _recorder()
    seen_models: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_models.append(json.loads(request.content)["model"])
        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)
    app.state.async_sleep = sleep

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "no-sleep-404"},
            json={"model": "opencodego-fast", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 404
    assert calls == []


# ---------------------------------------------------------------------------
# Config exposure
# ---------------------------------------------------------------------------


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
allowed_models:
  - fast
fallbacks:
  fast: []
retry_base_delay: 0.5
retry_max_delay: 5.0
""",
    )
    config = config_mod.load_route_config(config_path=str(cfg_path))
    assert config.retry_base_delay == 0.5
    assert config.retry_max_delay == 5.0

import json

import httpx
import pytest
from fastapi import FastAPI

from router.main import _fallback_session_id, create_app

ENV_OLLAMA = {"OLLAMA_API_BASE": "http://ollama", "OLLAMA_API_KEY": "x"}
ENV_GO = {"OPENCODE_GO_API_BASE": "http://go", "OPENCODE_GO_API_KEY": "x"}
ENV_DEEPSEEK = {"DEEPSEEK_API_KEY": "x"}
ENV_ALL = {**ENV_OLLAMA, **ENV_GO, **ENV_DEEPSEEK}


def _env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def _app(
    monkeypatch: pytest.MonkeyPatch,
    simple_route_config_path: str,
    env: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    if env is not None:
        _env(monkeypatch, env)
    return create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=transport,
        config_path=simple_route_config_path,
    )


@pytest.fixture
def upstream():
    """A mock upstream that serves queued responses in order."""

    class Upstream:
        def __init__(self) -> None:
            self.requests: list[httpx.Request] = []
            self._queue: list[httpx.Response] = []

        def _record(self, request: httpx.Request) -> None:
            self.requests.append(request)

        def enqueue_json(self, *, status_code: int = 200, body: dict | None = None) -> None:
            self._queue.append(httpx.Response(status_code, json=(body or {})))

        def enqueue_stream(
            self, *, status_code: int = 200, content: bytes, content_type: str = "text/event-stream"
        ) -> None:
            self._queue.append(httpx.Response(status_code, content=content, headers={"content-type": content_type}))

        def handler(self) -> httpx.MockTransport:
            async def _handler(request: httpx.Request) -> httpx.Response:
                self._record(request)
                if not self._queue:
                    return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
                return self._queue.pop(0)

            return httpx.MockTransport(_handler)

        def body(self, index: int = 0) -> dict:
            return json.loads(self.requests[index].content)

    return Upstream()


@pytest.mark.asyncio
async def test_healthz_reports_router_ok(simple_route_config_path: str):
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, config_path=simple_route_config_path)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["router"] == "ok"


@pytest.mark.asyncio
async def test_models_returns_live_catalog_combos_registry_and_connections(
    simple_route_config_path: str, monkeypatch: pytest.MonkeyPatch
):
    _env(monkeypatch, ENV_ALL)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, config_path=simple_route_config_path)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/models", headers={"Authorization": "Bearer test"})

    assert response.status_code == 200
    ids = [item["id"] for item in response.json()["data"]]
    assert "coder" in ids
    assert "kimi-k2.7-code" in ids
    assert "ollama-local.kimi-k2.7-code" in ids


@pytest.mark.asyncio
async def test_models_unknown_view_returns_400(simple_route_config_path: str):
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, config_path=simple_route_config_path)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/models?view=bad", headers={"Authorization": "Bearer test"})

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "gateway_catalog_view_invalid"


@pytest.mark.asyncio
async def test_chat_returns_422_for_empty_body(simple_route_config_path: str):
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, config_path=simple_route_config_path)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/chat/completions")

    assert response.status_code == 422
    assert response.json() == {"error": "request body must be valid JSON"}


@pytest.mark.asyncio
async def test_chat_retries_ordered_deployments_only_for_retryable_failures(
    monkeypatch, simple_route_config_path: str, upstream
):
    upstream.enqueue_json(status_code=429, body={"error": "rate limited"})
    upstream.enqueue_json(status_code=200, body={"choices": [{"message": {"content": "ok"}}]})
    app = _app(monkeypatch, simple_route_config_path, {**ENV_OLLAMA, **ENV_GO}, upstream.handler())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    assert upstream.body(0)["model"] == "ollama-local.kimi-k2.7-code"
    assert upstream.body(1)["model"] == "opencode-go.kimi-k2.7-code"


@pytest.mark.asyncio
async def test_chat_does_not_fallback_for_caller_error(monkeypatch, simple_route_config_path: str, upstream):
    upstream.enqueue_json(status_code=400, body={"error": "bad request"})
    app = _app(monkeypatch, simple_route_config_path, {**ENV_OLLAMA, **ENV_GO}, upstream.handler())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 400
    assert len(upstream.requests) == 1


@pytest.mark.asyncio
async def test_virtual_key_is_forwarded_with_rewritten_deployment_model(
    monkeypatch, simple_route_config_path: str, upstream
):
    upstream.enqueue_json(status_code=200, body={"choices": [{"message": {"content": "ok"}}]})
    app = _app(monkeypatch, simple_route_config_path, {**ENV_OLLAMA, **ENV_GO}, upstream.handler())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-virtual"},
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    assert upstream.requests[0].headers["authorization"] == "Bearer sk-virtual"
    assert upstream.body(0)["model"] == "ollama-local.kimi-k2.7-code"


@pytest.mark.asyncio
async def test_litellm_virtual_key_403_is_not_fallback(monkeypatch, simple_route_config_path: str, upstream):
    upstream.enqueue_json(
        status_code=403,
        body={"error": {"type": "key_model_access_denied", "message": "not allowed"}},
    )
    app = _app(monkeypatch, simple_route_config_path, {**ENV_OLLAMA, **ENV_GO}, upstream.handler())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-virtual"},
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 403
    assert len(upstream.requests) == 1


@pytest.mark.asyncio
async def test_no_x_session_id_does_not_pin_previous_deployment(monkeypatch, simple_route_config_path: str, upstream):
    upstream.enqueue_json(status_code=200, body={"choices": [{"message": {"content": "ok"}}]})
    upstream.enqueue_json(status_code=200, body={"choices": [{"message": {"content": "ok"}}]})
    app = _app(monkeypatch, simple_route_config_path, {**ENV_OLLAMA, **ENV_GO}, upstream.handler())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/v1/chat/completions",
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "a"}]},
        )
        app.state.routing_state.record_latency("ollama-local.kimi-k2.7-code", 900)
        app.state.routing_state.record_latency("opencode-go.kimi-k2.7-code", 100)
        await client.post(
            "/v1/chat/completions",
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "b"}]},
        )

    assert upstream.body(0)["model"] == "ollama-local.kimi-k2.7-code"
    assert upstream.body(1)["model"] == "opencode-go.kimi-k2.7-code"


@pytest.mark.asyncio
async def test_warm_session_is_scoped_by_auth_fingerprint(monkeypatch, simple_route_config_path: str, upstream):
    upstream.enqueue_json(status_code=200, body={"choices": [{"message": {"content": "ok"}}]})
    upstream.enqueue_json(status_code=200, body={"choices": [{"message": {"content": "ok"}}]})
    app = _app(monkeypatch, simple_route_config_path, {**ENV_OLLAMA, **ENV_GO}, upstream.handler())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer key-a", "X-Session-Id": "same"},
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "a"}]},
        )
        app.state.routing_state.record_latency("ollama-local.kimi-k2.7-code", 900)
        app.state.routing_state.record_latency("opencode-go.kimi-k2.7-code", 100)
        await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer key-b", "X-Session-Id": "same"},
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "b"}]},
        )

    assert upstream.body(0)["model"] == "ollama-local.kimi-k2.7-code"
    assert upstream.body(1)["model"] == "opencode-go.kimi-k2.7-code"


@pytest.mark.asyncio
async def test_warm_session_reuses_same_deployment_for_same_auth_and_session(
    monkeypatch, simple_route_config_path: str, upstream
):
    upstream.enqueue_json(status_code=200, body={"choices": [{"message": {"content": "ok"}}]})
    upstream.enqueue_json(status_code=200, body={"choices": [{"message": {"content": "ok"}}]})
    app = _app(monkeypatch, simple_route_config_path, {**ENV_OLLAMA, **ENV_GO}, upstream.handler())

    headers = {"Authorization": "Bearer key-a", "X-Session-Id": "same"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "a"}]},
        )
        app.state.routing_state.record_latency("ollama-local.kimi-k2.7-code", 900)
        app.state.routing_state.record_latency("opencode-go.kimi-k2.7-code", 100)
        await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "b"}]},
        )

    assert upstream.body(0)["model"] == "ollama-local.kimi-k2.7-code"
    assert upstream.body(1)["model"] == "ollama-local.kimi-k2.7-code"


@pytest.mark.asyncio
async def test_chat_success_sets_gateway_routing_headers(monkeypatch, simple_route_config_path: str, upstream):
    upstream.enqueue_json(status_code=200, body={"choices": [{"message": {"content": "ok"}}]})
    app = _app(monkeypatch, simple_route_config_path, {**ENV_OLLAMA, **ENV_GO}, upstream.handler())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    assert response.headers["X-Gateway-Requested-Model"] == "kimi-k2.7-code"
    assert response.headers["X-Gateway-Model-Kind"] == "registry-model"
    assert response.headers["X-Gateway-Served-Deployment"] == "ollama-local.kimi-k2.7-code"
    assert response.headers["X-Gateway-Fallback-Count"] == "0"


@pytest.mark.asyncio
async def test_no_active_deployments_returns_diagnostic_503(monkeypatch, simple_route_config_path: str):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not proxy")

    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=httpx.MockTransport(handler),
        config_path=simple_route_config_path,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 503
    payload = response.json()
    assert payload["error"]["type"] == "gateway_no_active_deployment"
    assert payload["error"]["model"] == "kimi-k2.7-code"
    inactive_reasons = payload["error"]["inactive_reasons"]
    assert isinstance(inactive_reasons, dict)
    assert "ollama-local.kimi-k2.7-code" in inactive_reasons
    assert inactive_reasons["ollama-local.kimi-k2.7-code"] == [
        "missing env OLLAMA_API_BASE",
        "missing env OLLAMA_API_KEY",
    ]
    assert "opencode-go.kimi-k2.7-code" in inactive_reasons
    assert inactive_reasons["opencode-go.kimi-k2.7-code"] == [
        "missing env OPENCODE_GO_API_BASE",
        "missing env OPENCODE_GO_API_KEY",
    ]


@pytest.mark.asyncio
async def test_transport_exhaustion_returns_502_gateway_upstream_exhausted(monkeypatch, simple_route_config_path: str):
    _env(monkeypatch, {**ENV_OLLAMA, **ENV_GO})

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=httpx.MockTransport(handler),
        config_path=simple_route_config_path,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["type"] == "gateway_upstream_exhausted"
    assert error["message"] == "All candidate deployments failed before a response stream started."
    assert error["model"] == "kimi-k2.7-code"
    assert error["attempted"] == ["ollama-local.kimi-k2.7-code", "opencode-go.kimi-k2.7-code"]
    assert error["last_status"] == "network_error"
    assert response.headers["X-Gateway-Fallback-Count"] == "2"
    assert response.headers["X-Gateway-Attempted-Models"] == ",".join(error["attempted"])


@pytest.mark.asyncio
async def test_http_failure_exhaustion_passes_through_upstream_error_with_gateway_headers(
    monkeypatch, simple_route_config_path: str, upstream
):
    upstream.enqueue_json(status_code=503, body={"error": {"type": "unavailable", "message": "try later"}})
    upstream.enqueue_json(status_code=503, body={"error": {"type": "unavailable", "message": "try later"}})
    app = _app(monkeypatch, simple_route_config_path, {**ENV_OLLAMA, **ENV_GO}, upstream.handler())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}]},
        )

    # Pass-through of the last upstream error body and status, with gateway headers attached.
    assert response.status_code == 503
    assert response.json() == {"error": {"type": "unavailable", "message": "try later"}}
    assert response.headers["X-Gateway-Requested-Model"] == "kimi-k2.7-code"
    assert response.headers["X-Gateway-Fallback-Count"] == "1"
    assert response.headers["X-Gateway-Attempted-Models"] == "ollama-local.kimi-k2.7-code,opencode-go.kimi-k2.7-code"


@pytest.mark.asyncio
async def test_unknown_explicit_model_returns_404(monkeypatch, simple_route_config_path: str):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not proxy")

    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=httpx.MockTransport(handler),
        config_path=simple_route_config_path,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "typo-model", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "gateway_model_not_found"


@pytest.mark.asyncio
async def test_unsupported_v1_path_returns_501():
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/responses", json={})

    assert response.status_code == 501


@pytest.mark.asyncio
async def test_missing_model_falls_back_to_default_model(monkeypatch, simple_route_config_path: str, upstream):
    upstream.enqueue_json(status_code=200, body={"choices": [{"message": {"content": "ok"}}]})
    app = _app(monkeypatch, simple_route_config_path, {**ENV_OLLAMA, **ENV_DEEPSEEK}, upstream.handler())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    assert response.headers["X-Gateway-Model-Kind"] == "combo"
    assert response.headers["X-Gateway-Requested-Model"] == "coder"


def test_fallback_session_id_is_stable_and_key_fingerprinted():
    body = {
        "messages": [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"},
        ]
    }
    token_a = "Bearer sk-test-key-a"
    token_b = "Bearer sk-test-key-b"

    id_a1 = _fallback_session_id(body, token_a)
    id_a2 = _fallback_session_id(body, token_a)
    id_b = _fallback_session_id(body, token_b)

    assert id_a1 == id_a2
    assert id_a1 != id_b
    assert len(id_a1) == 64
    assert token_a not in id_a1
    assert "sk-test-key" not in id_a1


def test_fallback_session_id_changes_with_messages():
    token = "Bearer sk-test-key"
    body_a = {"messages": [{"role": "user", "content": "Hello"}]}
    body_b = {"messages": [{"role": "user", "content": "Hi there"}]}

    assert _fallback_session_id(body_a, token) != _fallback_session_id(body_b, token)


def test_fallback_session_id_is_anonymous_without_messages():
    assert _fallback_session_id({}, "Bearer sk-test-key") == "anonymous"
    assert _fallback_session_id({"messages": []}, "Bearer sk-test-key") == "anonymous"


def test_create_app_defaults_when_no_config_file(monkeypatch, tmp_path):
    missing = tmp_path / "no-router.yaml"
    monkeypatch.setenv("ROUTER_CONFIG_PATH", str(missing))
    monkeypatch.setenv("LITELLM_CONFIG_PATH", str(tmp_path / "no-litellm.yaml"))

    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None)

    assert app.state.route_config.cache_ttl_seconds == 600
    assert app.state.route_config.default_model == "coder"
    assert app.state.route_config.combos == {}
    assert app.state.route_config.deployments == {}

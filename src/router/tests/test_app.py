import json

import httpx
import pytest

from router.main import _fallback_session_id, create_app


@pytest.mark.asyncio
async def test_healthz_reports_router_ok(simple_route_config_path: str):
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, config_path=simple_route_config_path)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["router"] == "ok"


@pytest.mark.asyncio
async def test_models_proxies_to_litellm(simple_route_config_path: str):
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"id": "explorer"}]})

    transport = httpx.MockTransport(handler)
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=transport,
        config_path=simple_route_config_path,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/models", headers={"Authorization": "Bearer test"})

    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "explorer"
    assert seen["url"] == "http://litellm:4000/v1/models"


@pytest.mark.asyncio
async def test_chat_rewrites_model_to_explicit_alias(simple_route_config_path: str):
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

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
            headers={"Authorization": "Bearer test", "X-Session-Id": "abc"},
            json={"model": "coder", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 200
    assert seen["json"]["model"] == "coder"
    assert response.headers["X-Gateway-Model"] == "coder"


@pytest.mark.asyncio
async def test_chat_returns_422_for_empty_body(simple_route_config_path: str):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("empty request body should not be proxied")

    transport = httpx.MockTransport(handler)
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=transport,
        config_path=simple_route_config_path,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/chat/completions")

    assert response.status_code == 422
    assert response.json() == {"error": "request body must be valid JSON"}


@pytest.mark.asyncio
async def test_chat_keeps_warm_session_model(simple_route_config_path: str):
    seen_models = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_models.append(json.loads(request.content)["model"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

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
            headers={"Authorization": "Bearer test", "X-Session-Id": "sticky"},
            json={"model": "coder", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )
        await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "sticky"},
            json={"messages": [{"role": "user", "content": "say hello"}]},
        )

    assert seen_models == ["coder", "coder"]


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


@pytest.mark.asyncio
async def test_chat_uses_stable_fallback_session_id(simple_route_config_path: str):
    seen_models = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_models.append(json.loads(request.content)["model"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

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
            headers={"Authorization": "Bearer sk-stable-key"},
            json={"model": "coder", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )
        # Same messages but different token should produce a different session ID.
        await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-other-key"},
            json={"model": "coder", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    # Both requests use the explicit model independently because sessions are keyed by token fingerprint.
    assert seen_models == ["coder", "coder"]

    # Sanity check the helper itself enforces key separation.
    body = {"messages": [{"role": "user", "content": "please refactor src/app.py"}]}
    assert _fallback_session_id(body, "Bearer sk-stable-key") != _fallback_session_id(body, "Bearer sk-other-key")


def test_fallback_session_id_changes_with_messages():
    token = "Bearer sk-test-key"
    body_a = {"messages": [{"role": "user", "content": "Hello"}]}
    body_b = {"messages": [{"role": "user", "content": "Hi there"}]}

    id_a = _fallback_session_id(body_a, token)
    id_b = _fallback_session_id(body_b, token)

    assert id_a != id_b


def test_fallback_session_id_is_anonymous_without_messages():
    assert _fallback_session_id({}, "Bearer sk-test-key") == "anonymous"
    assert _fallback_session_id({"messages": []}, "Bearer sk-test-key") == "anonymous"


def test_fallback_session_id_uses_structured_message_text():
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "please refactor src/app.py"},
                    {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
                ],
            }
        ]
    }

    id_a = _fallback_session_id(body, "Bearer sk-test-key-a")
    id_b = _fallback_session_id(body, "Bearer sk-test-key-b")

    assert id_a != "anonymous"
    assert id_a != id_b


def test_fallback_session_id_uses_token_when_message_has_no_text():
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
                ],
            }
        ]
    }

    id_a = _fallback_session_id(body, "Bearer sk-test-key-a")
    id_b = _fallback_session_id(body, "Bearer sk-test-key-b")

    assert id_a != "anonymous"
    assert id_a != id_b


@pytest.mark.asyncio
async def test_chat_does_not_write_session_on_failed_upstream(simple_route_config_path: str):
    seen_models: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        return httpx.Response(503, json={"error": "unavailable"})

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
            headers={"Authorization": "Bearer test", "X-Session-Id": "poison-test"},
            json={"model": "coder", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )
        assert response.status_code == 503
        # Whole coder chain exhausted.
        assert seen_models == ["coder", "explorer", "planner"]

        seen_models.clear()
        response2 = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "poison-test"},
            json={"model": "coder", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response2.status_code == 503
    # No session was written after the failed first request, so the second
    # request reclassifies from scratch instead of sticking to a failed model.
    assert seen_models == ["coder", "explorer", "planner"]
    assert response2.headers["X-Gateway-Reason"] == "explicit-model"


@pytest.mark.asyncio
async def test_chat_retries_next_fallback_on_retryable_error(simple_route_config_path: str):
    seen_models = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        if model == "coder":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

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
            headers={"Authorization": "Bearer test", "X-Session-Id": "retry-test"},
            json={"model": "coder", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 200
    assert seen_models == ["coder", "explorer"]
    assert response.headers["X-Gateway-Model"] == "explorer"
    assert response.headers["X-Gateway-Reason"] == "explicit-model"
    assert response.headers["X-Gateway-Fallback-From"] == "coder"
    assert response.headers["X-Gateway-Fallback-Count"] == "1"


@pytest.mark.asyncio
async def test_chat_does_not_retry_on_client_error(simple_route_config_path: str):
    seen_models = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_models.append(json.loads(request.content)["model"])
        return httpx.Response(400, json={"error": "bad request"})

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
            headers={"Authorization": "Bearer test", "X-Session-Id": "no-retry-test"},
            json={"model": "coder", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 400
    assert seen_models == ["coder"]
    assert "X-Gateway-Fallback-From" not in response.headers
    assert response.headers.get("X-Gateway-Fallback-Count") == "0"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "payload"),
    [
        (402, {"error": {"message": "billing hard limit reached"}}),
        (403, {"error": {"message": "subscription does not include this model"}}),
        (404, {"error": {"message": "model has been deprecated"}}),
        (400, {"error": {"message": "context length exceeded"}}),
        (400, {"error": {"message": "unsupported parameter: tools"}}),
    ],
)
async def test_chat_falls_back_on_provider_route_errors(
    simple_route_config_path: str,
    status_code: int,
    payload: dict,
):
    seen_models = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        if model == "coder":
            return httpx.Response(status_code, json=payload)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

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
            headers={"Authorization": "Bearer test", "X-Session-Id": f"fallback-{status_code}"},
            json={"model": "coder", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 200
    assert seen_models == ["coder", "explorer"]
    assert response.headers["X-Gateway-Model"] == "explorer"
    assert response.headers["X-Gateway-Fallback-From"] == "coder"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "payload"),
    [
        (401, {"error": {"message": "invalid api key"}}),
        (403, {"error": {"message": "Virtual key is not allowed to access model coder"}}),
        (429, {"error": {"message": "LiteLLM virtual key budget exceeded"}}),
        (400, {"error": {"message": "messages must contain at least one item"}}),
    ],
)
async def test_chat_does_not_fallback_on_caller_or_request_errors(
    simple_route_config_path: str,
    status_code: int,
    payload: dict,
):
    seen_models = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_models.append(json.loads(request.content)["model"])
        return httpx.Response(status_code, json=payload)

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
            headers={"Authorization": "Bearer test", "X-Session-Id": f"no-fallback-{status_code}"},
            json={"model": "coder", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == status_code
    assert seen_models == ["coder"]
    assert "X-Gateway-Fallback-From" not in response.headers
    assert response.headers.get("X-Gateway-Fallback-Count") == "0"


@pytest.mark.asyncio
async def test_chat_retries_on_timeout_exception(simple_route_config_path: str):
    seen_models = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        if model == "coder":
            raise httpx.TimeoutException("request timed out")
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

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
            headers={"Authorization": "Bearer test", "X-Session-Id": "timeout-test"},
            json={"model": "coder", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 200
    assert seen_models == ["coder", "explorer"]
    assert response.headers["X-Gateway-Fallback-Count"] == "1"


@pytest.mark.asyncio
async def test_chat_stores_fallback_count_in_session(simple_route_config_path: str):
    seen_models = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        if model == "coder":
            return httpx.Response(502, json={"error": "bad gateway"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

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
            headers={"Authorization": "Bearer test", "X-Session-Id": "fallback-count-test"},
            json={"model": "coder", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 200
    session = await app.state.session_store.get("fallback-count-test")
    assert session is not None
    assert session["model"] == "explorer"
    assert session["fallback_count"] == 1


@pytest.mark.asyncio
async def test_chat_fallback_from_warm_session_uses_warm_model_chain(simple_route_config_path: str):
    seen_models: list[str] = []
    fail_once: set[str] = set()

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        if model in fail_once:
            fail_once.discard(model)
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=transport,
        config_path=simple_route_config_path,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # First request: coder fails, falls back to explorer, succeeds.
        fail_once.add("coder")
        first = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "warm-chain"},
            json={"model": "coder", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )
        assert first.status_code == 200
        assert first.headers["X-Gateway-Model"] == "explorer"
        assert first.headers["X-Gateway-Fallback-Count"] == "1"

        # Second request: warm session keeps explorer; explorer fails; must fall back to
        # explorer-ocg (explorer's own first fallback), not give up because of the
        # stored fallback_count from the previous request.
        seen_models.clear()
        fail_once.add("explorer")
        second = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "warm-chain"},
            json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert second.status_code == 200
    assert seen_models == ["explorer", "planner"]
    assert second.headers["X-Gateway-Model"] == "planner"
    assert second.headers["X-Gateway-Reason"] == "warm-session"
    assert second.headers["X-Gateway-Fallback-From"] == "explorer"
    assert second.headers["X-Gateway-Fallback-Count"] == "1"


@pytest.mark.asyncio
async def test_chat_exhausted_fallback_returns_last_error_with_headers(simple_route_config_path: str):
    seen_models: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        return httpx.Response(503, json={"error": "unavailable"})

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
            headers={"Authorization": "Bearer test", "X-Session-Id": "exhausted"},
            json={"model": "coder", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 503
    assert seen_models == ["coder", "explorer", "planner"]
    assert response.headers["X-Gateway-Model"] == "planner"
    assert response.headers["X-Gateway-Fallback-From"] == "coder"
    assert response.headers["X-Gateway-Fallback-Count"] == "2"


@pytest.mark.asyncio
async def test_chat_exhausted_transport_errors_return_gateway_error_with_headers(simple_route_config_path: str):
    seen_models: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_models.append(json.loads(request.content)["model"])
        raise httpx.TimeoutException("request timed out")

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
            headers={"Authorization": "Bearer test", "X-Session-Id": "exhausted-timeouts"},
            json={"model": "coder", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 504
    assert response.json()["error"] == "upstream request failed"
    assert seen_models == ["coder", "explorer", "planner"]
    assert response.headers["X-Gateway-Model"] == "planner"
    assert response.headers["X-Gateway-Fallback-From"] == "coder"
    assert response.headers["X-Gateway-Fallback-Count"] == "2"


@pytest.mark.asyncio
async def test_unsupported_v1_path_returns_501():
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/responses", json={})

    assert response.status_code == 501


def test_create_app_loads_route_config_from_file(tmp_path):
    cfg_path = tmp_path / "router_config.yaml"
    cfg_path.write_text(
        """
cache_ttl_seconds: 42
default_model: explorer
allowed_models:
  - explorer
fallbacks:
  explorer: []
""",
    )
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        config_path=str(cfg_path),
        litellm_config_path=str(tmp_path / "missing-litellm.yaml"),
    )
    assert app.state.route_config.cache_ttl_seconds == 42
    assert app.state.route_config.allowed_models == {"explorer"}


def test_create_app_defaults_when_no_config_file(monkeypatch, tmp_path):
    # Point ROUTER_CONFIG_PATH at a missing file so we get defaults.
    missing = tmp_path / "no-router.yaml"
    monkeypatch.setenv("ROUTER_CONFIG_PATH", str(missing))
    monkeypatch.setenv("LITELLM_CONFIG_PATH", str(tmp_path / "no-litellm.yaml"))

    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None)

    assert app.state.route_config.cache_ttl_seconds == 600
    assert "explorer" in app.state.route_config.allowed_models

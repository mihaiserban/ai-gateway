import json

import httpx
import pytest

from router.main import _fallback_session_id, create_app


@pytest.mark.asyncio
async def test_healthz_reports_router_ok():
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["router"] == "ok"


@pytest.mark.asyncio
async def test_models_proxies_to_litellm():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"id": "fast"}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/models", headers={"Authorization": "Bearer test"})

    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "fast"
    assert seen["url"] == "http://litellm:4000/v1/models"


@pytest.mark.asyncio
async def test_chat_rewrites_model_to_classified_alias():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "abc"},
            json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 200
    assert seen["json"]["model"] == "opencodego-fast"
    assert response.headers["X-Gateway-Model"] == "opencodego-fast"


@pytest.mark.asyncio
async def test_chat_returns_422_for_empty_body():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("empty request body should not be proxied")

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/chat/completions")

    assert response.status_code == 422
    assert response.json() == {"error": "request body must be valid JSON"}


@pytest.mark.asyncio
async def test_chat_keeps_warm_session_model():
    seen_models = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_models.append(json.loads(request.content)["model"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "sticky"},
            json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )
        await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "sticky"},
            json={"messages": [{"role": "user", "content": "say hello"}]},
        )

    assert seen_models == ["opencodego-fast", "opencodego-fast"]


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
async def test_chat_uses_stable_fallback_session_id():
    seen_models = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_models.append(json.loads(request.content)["model"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-stable-key"},
            json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )
        # Same messages but different token should produce a different session ID.
        await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-other-key"},
            json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    # Both classify to opencodego-fast independently because sessions are keyed by token fingerprint.
    assert seen_models == ["opencodego-fast", "opencodego-fast"]

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
async def test_chat_does_not_write_session_on_failed_upstream():
    seen_models: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        return httpx.Response(503, json={"error": "unavailable"})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "poison-test"},
            json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )
        assert response.status_code == 503
        # Whole opencodego-fast chain exhausted.
        assert seen_models == ["opencodego-fast", "fast", "deepseek-pro"]

        seen_models.clear()
        response2 = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "poison-test"},
            json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response2.status_code == 503
    # No session was written after the failed first request, so the second
    # request reclassifies from scratch instead of sticking to a failed model.
    assert seen_models == ["opencodego-fast", "fast", "deepseek-pro"]
    assert response2.headers["X-Gateway-Reason"] == "classified"


@pytest.mark.asyncio
async def test_chat_retries_next_fallback_on_retryable_error():
    seen_models = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        if model == "opencodego-fast":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "retry-test"},
            json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 200
    assert seen_models == ["opencodego-fast", "fast"]
    assert response.headers["X-Gateway-Model"] == "fast"
    assert response.headers["X-Gateway-Reason"] == "classified"
    assert response.headers["X-Gateway-Fallback-From"] == "opencodego-fast"
    assert response.headers["X-Gateway-Fallback-Count"] == "1"


@pytest.mark.asyncio
async def test_chat_does_not_retry_on_client_error():
    seen_models = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_models.append(json.loads(request.content)["model"])
        return httpx.Response(400, json={"error": "bad request"})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "no-retry-test"},
            json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 400
    assert seen_models == ["opencodego-fast"]
    assert "X-Gateway-Fallback-From" not in response.headers
    assert response.headers.get("X-Gateway-Fallback-Count") == "0"


@pytest.mark.asyncio
async def test_chat_retries_on_timeout_exception():
    seen_models = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        if model == "opencodego-fast":
            raise httpx.TimeoutException("request timed out")
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "timeout-test"},
            json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 200
    assert seen_models == ["opencodego-fast", "fast"]
    assert response.headers["X-Gateway-Fallback-Count"] == "1"


@pytest.mark.asyncio
async def test_chat_stores_fallback_count_in_session():
    seen_models = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        if model == "opencodego-fast":
            return httpx.Response(502, json={"error": "bad gateway"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "fallback-count-test"},
            json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 200
    session = await app.state.session_store.get("fallback-count-test")
    assert session is not None
    assert session["model"] == "fast"
    assert session["fallback_count"] == 1


@pytest.mark.asyncio
async def test_chat_fallback_from_warm_session_uses_warm_model_chain():
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
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # First request: opencodego-fast fails, falls back to fast, succeeds.
        fail_once.add("opencodego-fast")
        first = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "warm-chain"},
            json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )
        assert first.status_code == 200
        assert first.headers["X-Gateway-Model"] == "fast"
        assert first.headers["X-Gateway-Fallback-Count"] == "1"

        # Second request: warm session keeps fast; fast fails; must fall back to
        # ollama-cloud (fast's own first fallback), not give up because of the
        # stored fallback_count from the previous request.
        seen_models.clear()
        fail_once.add("fast")
        second = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "warm-chain"},
            json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert second.status_code == 200
    assert seen_models == ["fast", "ollama-cloud"]
    assert second.headers["X-Gateway-Model"] == "ollama-cloud"
    assert second.headers["X-Gateway-Reason"] == "warm-session"
    assert second.headers["X-Gateway-Fallback-From"] == "fast"
    assert second.headers["X-Gateway-Fallback-Count"] == "1"


@pytest.mark.asyncio
async def test_chat_exhausted_fallback_returns_last_error_with_headers():
    seen_models: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        return httpx.Response(503, json={"error": "unavailable"})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "exhausted"},
            json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 503
    assert seen_models == ["opencodego-fast", "fast", "deepseek-pro"]
    assert response.headers["X-Gateway-Model"] == "deepseek-pro"
    assert response.headers["X-Gateway-Fallback-From"] == "opencodego-fast"
    assert response.headers["X-Gateway-Fallback-Count"] == "2"


@pytest.mark.asyncio
async def test_chat_exhausted_transport_errors_return_gateway_error_with_headers():
    seen_models: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_models.append(json.loads(request.content)["model"])
        raise httpx.TimeoutException("request timed out")

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "exhausted-timeouts"},
            json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 504
    assert response.json()["error"] == "upstream request failed"
    assert seen_models == ["opencodego-fast", "fast", "deepseek-pro"]
    assert response.headers["X-Gateway-Model"] == "deepseek-pro"
    assert response.headers["X-Gateway-Fallback-From"] == "opencodego-fast"
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
allowed_models:
  - fast
fallbacks:
  fast: []
""",
    )
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        config_path=str(cfg_path),
        litellm_config_path=str(tmp_path / "missing-litellm.yaml"),
    )
    assert app.state.route_config.cache_ttl_seconds == 42
    assert app.state.route_config.allowed_models == {"fast"}


def test_create_app_defaults_when_no_config_file(monkeypatch, tmp_path):
    # Point ROUTER_CONFIG_PATH at a missing file so we get defaults.
    missing = tmp_path / "no-router.yaml"
    monkeypatch.setenv("ROUTER_CONFIG_PATH", str(missing))
    monkeypatch.setenv("LITELLM_CONFIG_PATH", str(tmp_path / "no-litellm.yaml"))

    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None)

    assert app.state.route_config.cache_ttl_seconds == 600
    assert "fast" in app.state.route_config.allowed_models

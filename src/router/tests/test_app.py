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


@pytest.mark.asyncio
async def test_chat_does_not_write_session_on_failed_upstream():
    seen_models = []
    attempts_by_model: dict[str, int] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        attempts_by_model[model] = attempts_by_model.get(model, 0) + 1
        if model in {"opencodego-fast", "fast"} and attempts_by_model[model] == 1:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "poison-test"},
            json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )
        assert response.status_code == 503
        assert seen_models == ["opencodego-fast", "fast"]

        seen_models.clear()
        response2 = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "poison-test"},
            json={"messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response2.status_code == 200
    assert seen_models == ["opencodego-fast"]
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
async def test_unsupported_v1_path_returns_501():
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/responses", json={})

    assert response.status_code == 501

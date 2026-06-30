from __future__ import annotations

import json

import httpx
import pytest

from router.main import create_app

VIRTUAL_KEY = "sk-virtual-key-allowlisted"
MASTER_KEY = "Bearer sk-master-key"


@pytest.mark.asyncio
async def test_virtual_key_is_forwarded_to_litellm():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {VIRTUAL_KEY}", "X-Session-Id": "vkey-forward"},
            json={"model": "opencodego-fast", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 200
    assert seen["auth"] == f"Bearer {VIRTUAL_KEY}"


@pytest.mark.asyncio
async def test_virtual_key_allowlist_403_is_surfaced():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["model"] = json.loads(request.content)["model"]
        return httpx.Response(
            403,
            json={
                "error": {
                    "message": (
                        "key not allowed to access model. This key can only access "
                        "models=['fast']. Tried to access deepseek-pro"
                    ),
                    "type": "key_model_access_denied",
                    "code": "403",
                }
            },
        )

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {VIRTUAL_KEY}", "X-Session-Id": "vkey-block"},
            json={"model": "deepseek-pro", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 403
    assert seen["auth"] == f"Bearer {VIRTUAL_KEY}"
    assert seen["model"] == "deepseek-pro"
    body = response.json()
    assert "key_model_access_denied" in json.dumps(body)
    assert "X-Gateway-Fallback-From" not in response.headers
    assert response.headers.get("X-Gateway-Fallback-Count") == "0"


@pytest.mark.asyncio
async def test_master_key_smoke_still_works():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == MASTER_KEY
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=transport)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": MASTER_KEY, "X-Session-Id": "master-smoke"},
            json={"model": "opencodego-fast", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 200
    assert response.headers["X-Gateway-Model"] == "opencodego-fast"
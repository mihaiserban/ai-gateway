from __future__ import annotations

import json

import httpx
import pytest

from router.main import create_app

VIRTUAL_KEY = "sk-virtual-key-allowlisted"
MASTER_KEY = "Bearer sk-master-key"

ENV_OLLAMA = {"OLLAMA_API_BASE": "http://ollama", "OLLAMA_API_KEY": "x"}
ENV_GO = {"OPENCODE_GO_API_BASE": "http://go", "OPENCODE_GO_API_KEY": "x"}
ENV_DEEPSEEK = {"DEEPSEEK_API_KEY": "x"}
ENV_ALL = {**ENV_OLLAMA, **ENV_GO, **ENV_DEEPSEEK}


def _env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)


@pytest.mark.asyncio
async def test_virtual_key_is_forwarded_to_litellm(monkeypatch, simple_route_config_path: str):
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    _env(monkeypatch, ENV_ALL)
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=httpx.MockTransport(handler),
        config_path=simple_route_config_path,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {VIRTUAL_KEY}", "X-Session-Id": "vkey-forward"},
            json={"model": "coder", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    assert seen["auth"] == f"Bearer {VIRTUAL_KEY}"


@pytest.mark.asyncio
async def test_virtual_key_allowlist_403_is_surfaced(monkeypatch, simple_route_config_path: str):
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["model"] = json.loads(request.content)["model"]
        return httpx.Response(
            403,
            json={
                "error": {
                    "message": "key not allowed to access model",
                    "type": "key_model_access_denied",
                    "code": "403",
                }
            },
        )

    _env(monkeypatch, ENV_ALL)
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=httpx.MockTransport(handler),
        config_path=simple_route_config_path,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {VIRTUAL_KEY}", "X-Session-Id": "vkey-block"},
            json={"model": "coder", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 403
    assert seen["auth"] == f"Bearer {VIRTUAL_KEY}"
    assert seen["model"] == "ollama-cloud.kimi-k2.7-code"
    body = response.json()
    assert "key_model_access_denied" in json.dumps(body)
    assert response.headers.get("X-Gateway-Fallback-Count") == "0"


@pytest.mark.asyncio
async def test_master_key_smoke_still_works(monkeypatch, simple_route_config_path: str):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == MASTER_KEY
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    _env(monkeypatch, ENV_ALL)
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=httpx.MockTransport(handler),
        config_path=simple_route_config_path,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": MASTER_KEY, "X-Session-Id": "master-smoke"},
            json={"model": "coder", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    assert response.headers["X-Gateway-Served-Deployment"] == "ollama-cloud.kimi-k2.7-code"

import json

import httpx
import pytest

from router.main import create_app


@pytest.mark.asyncio
async def test_openai_chat_compat_preserves_auth_content_type_and_gateway_headers():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}}]})

    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=httpx.MockTransport(handler))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer virtual-key", "Content-Type": "application/json"},
            json={"model": "coder", "messages": [{"role": "user", "content": "say OK"}]},
        )

    assert response.status_code == 200
    assert seen["url"] == "http://litellm:4000/v1/chat/completions"
    assert seen["authorization"] == "Bearer virtual-key"
    assert seen["content_type"] == "application/json"
    assert seen["body"]["model"] == "coder"
    assert response.headers["X-Gateway-Model"] == "coder"


@pytest.mark.asyncio
async def test_models_compat_returns_live_gateway_catalog(
    simple_route_config_path: str,
    monkeypatch: pytest.MonkeyPatch,
):
    for key, value in {
        "OLLAMA_API_BASE": "http://ollama",
        "OLLAMA_API_KEY": "x",
        "DEEPSEEK_API_KEY": "x",
    }.items():
        monkeypatch.setenv(key, value)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("live catalog must not proxy to LiteLLM")

    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        config_path=simple_route_config_path,
        transport=httpx.MockTransport(handler),
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/models", headers={"Authorization": "Bearer virtual-key"})

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    ids = [item["id"] for item in body["data"]]
    assert "coder" in ids
    assert "kimi-k2.7-code" in ids
    assert "ollama-local.kimi-k2.7-code" in ids


@pytest.mark.asyncio
async def test_streaming_chat_compat_preserves_sse_content_type_and_body():
    sse_body = b'data: {"choices":[{"delta":{"content":"O"}}]}\n\ndata: [DONE]\n\n'

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse_body, headers={"content-type": "text/event-stream"})

    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=httpx.MockTransport(handler))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer virtual-key"},
            json={"model": "coder", "messages": [{"role": "user", "content": "say OK"}], "stream": True},
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    assert response.content == sse_body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("POST", "/v1/responses", {"model": "coder", "input": "say OK"}),
        ("POST", "/v1/embeddings", {"model": "text-embedding-3-small", "input": "hello"}),
        ("POST", "/v1/images/generations", {"model": "gpt-image-1", "prompt": "a square"}),
        ("GET", "/v1/files", None),
    ],
)
async def test_unsupported_openai_paths_return_clear_501(method, path, json_body):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unsupported paths must not be proxied")

    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=httpx.MockTransport(handler))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, path, json=json_body)

    assert response.status_code == 501
    assert response.json() == {"error": f"{path} is not implemented by the sticky router"}

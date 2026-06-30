from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from router.main import create_app


def _write_config(
    tmp_path: Path,
    *,
    cache_key_aliases: list[str],
    allowed_models: list[str],
    fallbacks: dict[str, list[str]],
) -> Path:
    cfg = tmp_path / "router_config.yaml"
    cfg.write_text(
        """
cache_ttl_seconds: 600
retry_base_delay: 0.0
retry_max_delay: 0.0
allowed_models:
"""
        + "".join(f"  - {m}\n" for m in allowed_models)
        + "fallbacks:\n"
        + "".join(f"  {k}:\n" + "".join(f"    - {v}\n" for v in vs) for k, vs in fallbacks.items())
        + "cache_key_aliases:\n"
        + "".join(f"  - {a}\n" for a in cache_key_aliases)
    )
    return cfg


def _expected_cache_key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


@pytest.mark.asyncio
async def test_cache_key_set_for_allowed_alias(tmp_path):
    cfg_path = _write_config(
        tmp_path,
        cache_key_aliases=["opencodego-fast"],
        allowed_models=["opencodego-fast", "fast", "deepseek-pro", "ollama-cloud"],
        fallbacks={
            "opencodego-fast": ["fast"],
            "fast": ["ollama-cloud"],
            "ollama-cloud": ["fast"],
            "deepseek-pro": ["fast"],
        },
    )
    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=transport,
        config_path=str(cfg_path),
        litellm_config_path=str(tmp_path / "missing-litellm.yaml"),
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "cache-session-1"},
            json={"model": "opencodego-fast", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 200
    assert seen[0]["model"] == "opencodego-fast"
    assert "prompt_cache_key" in seen[0]
    assert seen[0]["prompt_cache_key"] == _expected_cache_key("cache-session-1")


@pytest.mark.asyncio
async def test_cache_key_not_set_for_disallowed_alias(tmp_path):
    cfg_path = _write_config(
        tmp_path,
        cache_key_aliases=["deepseek-pro"],
        allowed_models=["opencodego-fast", "fast", "deepseek-pro", "ollama-cloud"],
        fallbacks={
            "opencodego-fast": ["fast"],
            "fast": ["ollama-cloud"],
            "ollama-cloud": ["fast"],
            "deepseek-pro": ["fast"],
        },
    )
    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=transport,
        config_path=str(cfg_path),
        litellm_config_path=str(tmp_path / "missing-litellm.yaml"),
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "cache-session-2"},
            json={"model": "opencodego-fast", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 200
    assert seen[0]["model"] == "opencodego-fast"
    assert "prompt_cache_key" not in seen[0]


@pytest.mark.asyncio
async def test_cache_key_stable_across_fallback(tmp_path):
    cfg_path = _write_config(
        tmp_path,
        cache_key_aliases=["opencodego-fast", "fast"],
        allowed_models=["opencodego-fast", "fast", "deepseek-pro", "ollama-cloud"],
        fallbacks={
            "opencodego-fast": ["fast"],
            "fast": ["ollama-cloud"],
            "ollama-cloud": ["fast"],
            "deepseek-pro": ["fast"],
        },
    )
    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if body["model"] == "opencodego-fast":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=transport,
        config_path=str(cfg_path),
        litellm_config_path=str(tmp_path / "missing-litellm.yaml"),
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "cache-session-3"},
            json={"model": "opencodego-fast", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 200
    assert [b["model"] for b in seen] == ["opencodego-fast", "fast"]
    expected = _expected_cache_key("cache-session-3")
    assert seen[0]["prompt_cache_key"] == expected
    assert seen[1]["prompt_cache_key"] == expected
    assert seen[0]["prompt_cache_key"] == seen[1]["prompt_cache_key"]


@pytest.mark.asyncio
async def test_cache_key_absent_by_default(tmp_path):
    cfg_path = _write_config(
        tmp_path,
        cache_key_aliases=[],
        allowed_models=["opencodego-fast", "fast", "deepseek-pro", "ollama-cloud"],
        fallbacks={
            "opencodego-fast": ["fast"],
            "fast": ["ollama-cloud"],
            "ollama-cloud": ["fast"],
            "deepseek-pro": ["fast"],
        },
    )
    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=transport,
        config_path=str(cfg_path),
        litellm_config_path=str(tmp_path / "missing-litellm.yaml"),
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "cache-session-4"},
            json={"model": "opencodego-fast", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 200
    assert "prompt_cache_key" not in seen[0]


@pytest.mark.asyncio
async def test_cache_key_removed_on_fallback_to_non_allowed_alias(tmp_path):
    cfg_path = _write_config(
        tmp_path,
        cache_key_aliases=["opencodego-fast"],
        allowed_models=["opencodego-fast", "fast", "deepseek-pro", "ollama-cloud"],
        fallbacks={
            "opencodego-fast": ["ollama-cloud"],
            "fast": ["ollama-cloud"],
            "ollama-cloud": ["fast"],
            "deepseek-pro": ["fast"],
        },
    )
    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if body["model"] == "opencodego-fast":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    transport = httpx.MockTransport(handler)
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=transport,
        config_path=str(cfg_path),
        litellm_config_path=str(tmp_path / "missing-litellm.yaml"),
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "cache-session-5"},
            json={"model": "opencodego-fast", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 200
    assert [b["model"] for b in seen] == ["opencodego-fast", "ollama-cloud"]
    # The allowed alias attempt carried the cache key.
    assert seen[0]["prompt_cache_key"] == _expected_cache_key("cache-session-5")
    # The non-allowed fallback alias must not carry it.
    assert "prompt_cache_key" not in seen[1]


@pytest.mark.asyncio
async def test_cache_response_headers_are_forwarded(tmp_path):
    cfg_path = _write_config(
        tmp_path,
        cache_key_aliases=[],
        allowed_models=["opencodego-fast", "fast", "deepseek-pro", "ollama-cloud"],
        fallbacks={
            "opencodego-fast": ["fast"],
            "fast": ["ollama-cloud"],
            "ollama-cloud": ["fast"],
            "deepseek-pro": ["fast"],
        },
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "OK"}}]},
            headers={
                "x-litellm-cache-hit": "true",
                "x-litellm-cache-key": "abc123",
                "x-unrelated-upstream": "drop-me",
            },
        )

    transport = httpx.MockTransport(handler)
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=transport,
        config_path=str(cfg_path),
        litellm_config_path=str(tmp_path / "missing-litellm.yaml"),
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "cache-response-headers"},
            json={"model": "opencodego-fast", "messages": [{"role": "user", "content": "please refactor src/app.py"}]},
        )

    assert response.status_code == 200
    assert response.headers["x-litellm-cache-hit"] == "true"
    assert response.headers["x-litellm-cache-key"] == "abc123"
    assert "x-unrelated-upstream" not in response.headers

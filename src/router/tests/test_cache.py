from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from router.main import create_app

ENV_OLLAMA = {"OLLAMA_API_BASE": "http://ollama", "OLLAMA_API_KEY": "x"}
ENV_GO = {"OPENCODE_GO_API_BASE": "http://go", "OPENCODE_GO_API_KEY": "x"}


def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {**ENV_OLLAMA, **ENV_GO}.items():
        monkeypatch.setenv(key, value)


def _write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "router_config.yaml"
    cfg.write_text(
        """
cache_ttl_seconds: 600
retry_base_delay: 0.0
retry_max_delay: 0.0
default_model: coder
combos:
  coder:
    strategy: score
    candidates:
      - ollama-local.kimi-k2.7-code
deployments:
  ollama-local.kimi-k2.7-code:
    provider: ollama
    connection: ollama-local
    model: kimi-k2.7-code
    required_env:
      - OLLAMA_API_BASE
      - OLLAMA_API_KEY
registry_models:
  kimi-k2.7-code:
    - ollama-local.kimi-k2.7-code
""",
    )
    return cfg


@pytest.mark.asyncio
async def test_cache_response_headers_are_forwarded(monkeypatch, tmp_path):
    cfg_path = _write_config(tmp_path)
    _env(monkeypatch)

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

    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=httpx.MockTransport(handler),
        config_path=str(cfg_path),
        litellm_config_path=str(tmp_path / "missing-litellm.yaml"),
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "cache-response-headers"},
            json={"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    assert response.headers["x-litellm-cache-hit"] == "true"
    assert response.headers["x-litellm-cache-key"] == "abc123"
    assert "x-unrelated-upstream" not in response.headers

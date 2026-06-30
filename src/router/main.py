from __future__ import annotations

import hashlib
import os
import time
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from router.redaction import redact_payload
from router.routing import RouteConfig, choose_model
from router.sessions import MemorySessionStore, RedisSessionStore, SessionStore


FORWARDED_HEADERS = {"authorization", "content-type", "accept"}


def create_app(
    *,
    litellm_base_url: str | None = None,
    redis_url: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    app = FastAPI(title="Personal AI Gateway Router")
    app.state.litellm_base_url = (litellm_base_url or os.environ.get("LITELLM_BASE_URL") or "http://litellm:4000").rstrip("/")
    app.state.redis_url = redis_url if redis_url is not None else os.environ.get("REDIS_URL")
    app.state.transport = transport
    app.state.route_config = RouteConfig()
    app.state.session_store = _session_store(app.state.redis_url)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"router": "ok"}

    @app.get("/v1/models")
    async def models(request: Request) -> Response:
        return await _proxy(request, "GET", "/v1/models")

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        body = await request.json()
        token = request.headers.get("authorization")
        session_id = request.headers.get("X-Session-Id") or _fallback_session_id(body, token)
        session = await app.state.session_store.get(session_id)
        decision = choose_model(body, session=session, now=time.time(), config=app.state.route_config)

        upstream_body = redact_payload(body)
        upstream_body["model"] = decision.model

        await app.state.session_store.set(
            session_id,
            {
                "model": decision.model,
                "last_used_ts": time.time(),
                "classification": decision.reason,
                "fallback_count": 0,
            },
            ttl_seconds=app.state.route_config.cache_ttl_seconds,
        )

        return await _proxy_json(
            request,
            "/v1/chat/completions",
            upstream_body,
            extra_headers={"X-Gateway-Model": decision.model, "X-Gateway-Reason": decision.reason},
        )

    @app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def unsupported_v1_path(path: str) -> JSONResponse:
        return JSONResponse(
            status_code=501,
            content={"error": f"/v1/{path} is not implemented by the sticky router"},
        )

    async def _proxy(request: Request, method: str, path: str) -> Response:
        url = app.state.litellm_base_url + path
        async with httpx.AsyncClient(transport=app.state.transport, timeout=120) as client:
            upstream = await client.request(method, url, headers=_forward_headers(request))
        return _response_from_upstream(upstream)

    async def _proxy_json(
        request: Request,
        path: str,
        body: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
    ) -> Response:
        url = app.state.litellm_base_url + path
        async with httpx.AsyncClient(transport=app.state.transport, timeout=120) as client:
            upstream = await client.post(url, headers=_forward_headers(request), json=body)
        return _response_from_upstream(upstream, extra_headers=extra_headers)

    return app


def _forward_headers(request: Request) -> dict[str, str]:
    return {
        key: value
        for key, value in request.headers.items()
        if key.lower() in FORWARDED_HEADERS
    }


def _response_from_upstream(
    upstream: httpx.Response,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    content_type = upstream.headers.get("content-type", "application/json")
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=content_type,
        headers=extra_headers,
    )


def _fallback_session_id(body: dict[str, Any], token: str | None = None) -> str:
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return "anonymous"

    system_text = ""
    user_text = ""
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if role == "system" and isinstance(content, str) and not system_text:
            system_text = content
        elif role == "user" and isinstance(content, str) and not user_text:
            user_text = content
        if system_text and user_text:
            break

    if not system_text and not user_text:
        return "anonymous"

    token_fingerprint = ""
    if isinstance(token, str):
        token_fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()

    digest = hashlib.sha256(
        f"{token_fingerprint}:{system_text}:{user_text}".encode("utf-8")
    ).hexdigest()
    return digest


def _session_store(redis_url: str | None) -> SessionStore:
    if redis_url:
        return RedisSessionStore(redis_url)
    return MemorySessionStore()


app = create_app()

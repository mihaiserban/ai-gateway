from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from router.config import load_and_validate
from router.health import all_ready, gather_health
from router.metrics import Metrics
from router.redaction import redact_payload
from router.routing import _timeout_for, choose_model, next_fallback
from router.sessions import MemorySessionStore, RedisSessionStore, SessionStore

FORWARDED_HEADERS = {"authorization", "content-type", "accept"}
CACHE_RESPONSE_HEADERS = {
    "x-litellm-cache-hit",
    "x-litellm-cache-key",
}

logger = logging.getLogger("router")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _router_handler = logging.StreamHandler()
    _router_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_router_handler)


def create_app(
    *,
    litellm_base_url: str | None = None,
    redis_url: str | None = None,
    database_url: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    config_path: str | None = None,
    litellm_config_path: str | None = None,
) -> FastAPI:
    app = FastAPI(title="Personal AI Gateway Router")
    app.state.litellm_base_url = (
        litellm_base_url or os.environ.get("LITELLM_BASE_URL") or "http://litellm:4000"
    ).rstrip("/")
    app.state.redis_url = redis_url if redis_url is not None else os.environ.get("REDIS_URL")
    app.state.database_url = database_url if database_url is not None else os.environ.get("DATABASE_URL")
    app.state.transport = transport
    app.state.route_config = load_and_validate(
        config_path=config_path,
        litellm_path=litellm_config_path,
    )
    app.state.session_store = _session_store(app.state.redis_url)
    app.state.metrics = Metrics()
    app.state.async_sleep = asyncio.sleep

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        statuses = await gather_health(app.state)
        return JSONResponse(status_code=200, content=statuses)

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        statuses = await gather_health(app.state)
        if all_ready(statuses):
            statuses["status"] = "ready"
            return JSONResponse(status_code=200, content=statuses)
        statuses["status"] = "not ready"
        return JSONResponse(status_code=503, content=statuses)

    @app.get("/metrics")
    async def metrics() -> JSONResponse:
        return JSONResponse(status_code=200, content=app.state.metrics.snapshot())

    @app.get("/v1/models")
    async def models(request: Request) -> Response:
        return await _proxy(request, "GET", "/v1/models")

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        start = time.perf_counter()
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(status_code=422, content={"error": "request body must be valid JSON"})
        if not isinstance(body, dict):
            return JSONResponse(status_code=422, content={"error": "request body must be a JSON object"})
        token = request.headers.get("authorization")
        session_id = request.headers.get("X-Session-Id") or _fallback_session_id(body, token)
        session = await app.state.session_store.get(session_id)
        decision = choose_model(body, session=session, now=time.time(), config=app.state.route_config)

        upstream_body = redact_payload(body)
        is_stream = bool(body.get("stream"))

        cache_key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]

        original_model = decision.model
        current_model = original_model
        last_response: httpx.Response | None = None
        last_exception: Exception | None = None
        attempt = 0
        tried: set[str] = {original_model}

        while True:
            upstream_body["model"] = current_model
            if current_model in app.state.route_config.cache_key_aliases:
                upstream_body["prompt_cache_key"] = cache_key
            elif "prompt_cache_key" in upstream_body:
                del upstream_body["prompt_cache_key"]
            try:
                if is_stream:
                    response = await _proxy_stream(request, "/v1/chat/completions", upstream_body, current_model)
                else:
                    response = await _proxy_json(request, "/v1/chat/completions", upstream_body, current_model)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_exception = exc
                last_response = None
                app.state.metrics.record_provider_attempt(
                    current_model,
                    _exception_status(exc),
                    success=False,
                    retryable_failure=True,
                )
            else:
                last_response = response
                last_exception = None
                app.state.metrics.record_provider_attempt(
                    current_model,
                    response.status_code,
                    success=_is_success(response),
                    retryable_failure=_is_retryable_status(response.status_code),
                )
                if _is_success(response):
                    break
                if not _is_retryable_status(response.status_code):
                    break
                if is_stream:
                    client = getattr(response, "_stream_client", None)
                    await response.aclose()
                    if client is not None:
                        await client.aclose()

            next_model = next_fallback(original_model, attempt, app.state.route_config)
            if next_model is None or next_model in tried:
                break
            delay = min(
                app.state.route_config.retry_base_delay * (2**attempt),
                app.state.route_config.retry_max_delay,
            )
            await app.state.async_sleep(delay)
            current_model = next_model
            tried.add(current_model)
            attempt += 1

        fallback_count = attempt
        latency_ms = int((time.perf_counter() - start) * 1000)
        provider_model = app.state.route_config.provider_models.get(current_model, "")

        if last_response is not None and _is_success(last_response):
            await app.state.session_store.set(
                session_id,
                {
                    "model": current_model,
                    "last_used_ts": time.time(),
                    "reason": decision.reason,
                    "fallback_count": fallback_count,
                },
                ttl_seconds=app.state.route_config.cache_ttl_seconds,
            )

        extra_headers = {
            "X-Gateway-Model": current_model,
            "X-Gateway-Provider-Model": provider_model,
            "X-Gateway-Reason": decision.reason,
            "X-Gateway-Fallback-Count": str(fallback_count),
        }
        if fallback_count > 0:
            extra_headers["X-Gateway-Fallback-From"] = original_model

        if last_response is not None:
            status = last_response.status_code
        else:
            status = 504 if isinstance(last_exception, httpx.TimeoutException) else 502
        _log_request(
            session_id,
            current_model,
            provider_model,
            decision.reason,
            status,
            latency_ms,
            fallback_count,
            original_model,
        )
        cache_hit = _cache_hit(last_response) if last_response is not None else None
        app.state.metrics.record(original_model, current_model, fallback_count, cache_hit=cache_hit)

        if last_response is not None:
            if is_stream and _is_success(last_response):
                return _streaming_response(last_response, extra_headers=extra_headers)
            if is_stream:
                content = await last_response.aread()
                client = getattr(last_response, "_stream_client", None)
                await last_response.aclose()
                if client is not None:
                    await client.aclose()
                return Response(
                    content=content,
                    status_code=last_response.status_code,
                    media_type=last_response.headers.get("content-type", "application/json"),
                    headers=extra_headers,
                )
            return _response_from_upstream(last_response, extra_headers=extra_headers)

        return JSONResponse(
            status_code=status,
            content={"error": "upstream request failed"},
            headers=extra_headers,
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
        model: str,
    ) -> httpx.Response:
        url = app.state.litellm_base_url + path
        timeout = _timeout_for(app.state.route_config, model)
        async with httpx.AsyncClient(transport=app.state.transport, timeout=timeout) as client:
            return await client.post(url, headers=_forward_headers(request), json=body)

    async def _proxy_stream(
        request: Request,
        path: str,
        body: dict[str, Any],
        model: str,
    ) -> httpx.Response:
        url = app.state.litellm_base_url + path
        timeout = _timeout_for(app.state.route_config, model)
        client = httpx.AsyncClient(transport=app.state.transport, timeout=timeout)
        response = await client.send(
            client.build_request("POST", url, headers=_forward_headers(request), json=body),
            stream=True,
        )
        response._stream_client = client  # type: ignore[attr-defined]
        return response

    return app


def _is_success(response: httpx.Response) -> bool:
    return 200 <= response.status_code < 300


def _log_request(
    session_id: str,
    model: str,
    provider_model: str,
    reason: str,
    status: int | str,
    latency_ms: int,
    fallback_count: int,
    fallback_from: str,
) -> None:
    session_id_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
    parts = [
        f"session_id_hash={session_id_hash}",
        f"model={model}",
    ]
    if provider_model:
        parts.append(f"provider_model={provider_model}")
    parts.extend([
        f"reason={reason}",
        f"status={status}",
        f"latency_ms={latency_ms}",
        f"fallback_count={fallback_count}",
    ])
    if fallback_count > 0:
        parts.append(f"fallback_from={fallback_from}")
    logger.info(" ".join(parts))


def _is_retryable_status(status_code: int) -> bool:
    return status_code in {429, 500, 502, 503, 504}


def _exception_status(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.NetworkError):
        return "network_error"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "remote_protocol_error"
    return "upstream_error"


def _cache_hit(upstream: httpx.Response) -> bool | None:
    value = upstream.headers.get("x-litellm-cache-hit")
    if value is None:
        if upstream.headers.get("x-litellm-cache-key") is not None:
            return False
        return None
    return value.lower() in {"1", "true", "yes"}


def _forward_headers(request: Request) -> dict[str, str]:
    return {key: value for key, value in request.headers.items() if key.lower() in FORWARDED_HEADERS}


def _response_from_upstream(
    upstream: httpx.Response,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    content_type = upstream.headers.get("content-type", "application/json")
    headers = _response_headers(upstream, extra_headers)
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=content_type,
        headers=headers,
    )


def _streaming_response(
    upstream: httpx.Response,
    extra_headers: dict[str, str] | None = None,
) -> StreamingResponse:
    content_type = upstream.headers.get("content-type", "text/event-stream")
    headers = _response_headers(upstream, extra_headers)
    headers["content-type"] = content_type

    async def body_iterator() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            client = getattr(upstream, "_stream_client", None)
            await upstream.aclose()
            if client is not None:
                await client.aclose()

    return StreamingResponse(
        content=body_iterator(),
        status_code=upstream.status_code,
        headers=headers,
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
        text = _content_text(content)
        if role == "system" and text and not system_text:
            system_text = text
        elif role == "user" and text and not user_text:
            user_text = text
        if system_text and user_text:
            break

    token_fingerprint = ""
    if isinstance(token, str):
        token_fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()

    if not system_text and not user_text and not token_fingerprint:
        return "anonymous"

    return hashlib.sha256(f"{token_fingerprint}:{system_text}:{user_text}".encode()).hexdigest()


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        text = part.get("text") or part.get("content")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _response_headers(
    upstream: httpx.Response,
    extra_headers: dict[str, str] | None,
) -> dict[str, str]:
    headers = dict(extra_headers or {})
    for key, value in upstream.headers.items():
        if key.lower() in CACHE_RESPONSE_HEADERS:
            headers[key] = value
    return headers


def _session_store(redis_url: str | None) -> SessionStore:
    if redis_url:
        return RedisSessionStore(redis_url)
    return MemorySessionStore()


app = create_app()

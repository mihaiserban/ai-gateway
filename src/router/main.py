from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import nullcontext
from typing import Any, cast

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from router.config import load_and_validate
from router.dashboard import register_dashboard
from router.health import all_ready, gather_health
from router.metrics import Metrics
from router.redaction import redact_payload
from router.routing import _timeout_for, choose_model, next_fallback
from router.sessions import MemorySessionStore, RedisSessionStore, SessionStore
from router.usage_events import HttpUsageEventSink, UsageEvent, estimate_cost_usd, extract_usage, fingerprint

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
    usage_sink: Any | None = None,
) -> FastAPI:
    app = FastAPI(title="Personal AI Gateway Router")
    app.state.litellm_base_url = (
        litellm_base_url or os.environ.get("LITELLM_BASE_URL") or "http://litellm:4000"
    ).rstrip("/")
    app.state.redis_url = redis_url if redis_url is not None else os.environ.get("REDIS_URL")
    app.state.database_url = database_url if database_url is not None else os.environ.get("DATABASE_URL")
    app.state.transport = transport
    app.state.http_client = httpx.AsyncClient(transport=transport)
    app.state.route_config = load_and_validate(
        config_path=config_path,
        litellm_path=litellm_config_path,
    )
    app.state.upstream_semaphore = (
        asyncio.Semaphore(app.state.route_config.max_concurrent_upstream)
        if app.state.route_config.max_concurrent_upstream > 0
        else None
    )
    app.state.session_store = _session_store(app.state.redis_url)
    app.state.metrics = Metrics()
    app.state.usage_sink = usage_sink or HttpUsageEventSink(os.environ.get("USAGE_LEDGER_URL"))
    app.state.async_sleep = asyncio.sleep
    register_dashboard(app)

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

        def _provider_model(model: str) -> str:
            return str(app.state.route_config.provider_models.get(model, model))

        while True:
            upstream_body["model"] = current_model
            if is_stream:
                upstream_body["stream_options"] = {
                    **(upstream_body.get("stream_options") or {}),
                    "include_usage": True,
                }
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
                    provider_model=_provider_model(current_model),
                )
            else:
                last_response = response
                last_exception = None
                if is_stream and not _is_success(response):
                    await response.aread()
                should_fallback = _should_fallback_response(response)
                app.state.metrics.record_provider_attempt(
                    current_model,
                    response.status_code,
                    success=_is_success(response),
                    retryable_failure=should_fallback,
                    provider_model=_provider_model(current_model),
                )
                if _is_success(response):
                    break
                if not should_fallback:
                    break
                if is_stream:
                    await response.aclose()

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
        provider_model = app.state.route_config.provider_models.get(current_model, "")
        latency_ms = int((time.perf_counter() - start) * 1000)

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

        upstream_payload: dict[str, Any] | None = None
        if last_response is not None and not is_stream:
            try:
                upstream_payload = last_response.json()
            except Exception:
                upstream_payload = None

        streaming_success = is_stream and last_response is not None and _is_success(last_response)

        if not streaming_success:
            await _emit_usage_event(
                app,
                request=request,
                session_id=session_id,
                requested_model=decision.model,
                selected_model=original_model,
                served_model=current_model,
                provider_model=provider_model,
                reason=decision.reason,
                status=status,
                latency_ms=latency_ms,
                fallback_count=fallback_count,
                fallback_from=original_model,
                cache_hit=cache_hit,
                payload=upstream_payload,
                error_class=_exception_class(last_exception),
                stream=is_stream,
            )

        if last_response is not None:
            if is_stream and _is_success(last_response):
                return _streaming_response(
                    last_response,
                    extra_headers=extra_headers,
                    on_close=_make_usage_event_callback(
                        app,
                        request=request,
                        session_id=session_id,
                        requested_model=decision.model,
                        selected_model=original_model,
                        served_model=current_model,
                        provider_model=provider_model,
                        reason=decision.reason,
                        status=status,
                        latency_ms=latency_ms,
                        fallback_count=fallback_count,
                        fallback_from=original_model,
                        cache_hit=cache_hit,
                        error_class=_exception_class(last_exception),
                        stream=is_stream,
                    ),
                )
            if is_stream:
                content = await last_response.aread()
                await last_response.aclose()
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
        guard = app.state.upstream_semaphore or nullcontext()
        async with guard:
            upstream = await app.state.http_client.request(method, url, headers=_forward_headers(request), timeout=120)
        return _response_from_upstream(upstream)

    async def _proxy_json(
        request: Request,
        path: str,
        body: dict[str, Any],
        model: str,
    ) -> httpx.Response:
        url = app.state.litellm_base_url + path
        timeout = _timeout_for(app.state.route_config, model)
        guard = app.state.upstream_semaphore or nullcontext()
        async with guard:
            return cast(
                httpx.Response,
                await app.state.http_client.post(url, headers=_forward_headers(request), json=body, timeout=timeout),
            )

    async def _proxy_stream(
        request: Request,
        path: str,
        body: dict[str, Any],
        model: str,
    ) -> httpx.Response:
        url = app.state.litellm_base_url + path
        timeout = _timeout_for(app.state.route_config, model)
        guard = app.state.upstream_semaphore or nullcontext()
        async with guard:
            req = app.state.http_client.build_request("POST", url, headers=_forward_headers(request), json=body)
            req.extensions = {"timeout": {"connect": timeout, "read": timeout, "write": timeout, "pool": timeout}}
            return cast(httpx.Response, await app.state.http_client.send(req, stream=True))

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
    parts.extend(
        [
            f"reason={reason}",
            f"status={status}",
            f"latency_ms={latency_ms}",
            f"fallback_count={fallback_count}",
        ]
    )
    if fallback_count > 0:
        parts.append(f"fallback_from={fallback_from}")
    logger.info(" ".join(parts))


def _should_fallback_response(response: httpx.Response) -> bool:
    status_code = response.status_code
    if 200 <= status_code < 300:
        return False
    if status_code in {500, 502, 503, 504}:
        return True

    message = _error_text(response)
    if _has_any(message, CALLER_LIMIT_ERROR_MARKERS):
        return False

    if status_code in {402, 429}:
        return True
    if status_code == 403:
        return _has_any(message, PROVIDER_ACCESS_ERROR_MARKERS)
    if status_code == 404:
        return _has_any(message, PROVIDER_MODEL_ERROR_MARKERS)
    if status_code == 400:
        return _has_any(message, PROVIDER_REQUEST_ERROR_MARKERS)
    return False


CALLER_LIMIT_ERROR_MARKERS = (
    "virtual key",
    "allowed models",
    "not allowed to access",
)

PROVIDER_ACCESS_ERROR_MARKERS = (
    "subscription",
    "entitlement",
    "does not include",
    "do not have access",
    "don't have access",
    "region",
    "not enabled",
)

PROVIDER_MODEL_ERROR_MARKERS = (
    "model",
    "deployment",
    "deprecated",
)

PROVIDER_REQUEST_ERROR_MARKERS = (
    "context length",
    "maximum context",
    "too many tokens",
    "unsupported parameter",
    "unsupported param",
    "unsupported tool",
    "tools not supported",
    "functions not supported",
)


def _error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        return response.text.lower()
    return json.dumps(payload, sort_keys=True).lower()


def _has_any(value: str, markers: tuple[str, ...]) -> bool:
    return any(marker in value for marker in markers)


def _exception_status(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.NetworkError):
        return "network_error"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "remote_protocol_error"
    return "upstream_error"


def _exception_class(exc: Exception | None) -> str | None:
    if exc is None:
        return None
    return type(exc).__name__


def _cache_status(cache_hit: bool | None) -> str:
    if cache_hit is True:
        return "hit"
    if cache_hit is False:
        return "miss"
    return "unknown"


async def _emit_usage_event(
    app: FastAPI,
    *,
    request: Request,
    session_id: str,
    requested_model: str | None,
    selected_model: str,
    served_model: str,
    provider_model: str,
    reason: str,
    status: int | str,
    latency_ms: int,
    fallback_count: int,
    fallback_from: str,
    cache_hit: bool | None,
    payload: dict[str, Any] | None,
    error_class: str | None,
    stream: bool,
) -> None:
    prompt_tokens, completion_tokens, total_tokens = extract_usage(payload)
    event = UsageEvent(
        timestamp=time.time(),
        path=str(request.url.path),
        method=request.method,
        key_hash=fingerprint(request.headers.get("authorization")),
        session_hash=fingerprint(session_id),
        requested_model=requested_model,
        selected_model=selected_model,
        served_model=served_model,
        provider_model=provider_model,
        reason=reason,
        status=str(status),
        latency_ms=latency_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        estimated_cost_usd=estimate_cost_usd(
            served_model, prompt_tokens, completion_tokens, app.state.route_config.model_prices
        ),
        cache_status=_cache_status(cache_hit),
        fallback_count=fallback_count,
        fallback_from=fallback_from if fallback_count > 0 else None,
        error_class=error_class,
        stream=stream,
    )
    try:
        await app.state.usage_sink.record(event)
    except Exception:
        logger.exception("usage_event_sink_failed")


def _make_usage_event_callback(app: FastAPI, **kwargs: Any) -> Callable[[dict[str, Any] | None], Awaitable[None]]:
    async def _callback(payload: dict[str, Any] | None) -> None:
        await _emit_usage_event(app, payload=payload, **kwargs)

    return _callback


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


def _extract_usage_from_sse(content: bytes) -> dict[str, Any] | None:
    if not content:
        return None
    text = content.decode("utf-8", errors="replace")
    for event in text.split("\n\n"):
        for line in event.splitlines():
            line = line.strip()
            if not line.startswith("data: "):
                continue
            data = line[6:].strip()
            if data == "[DONE]":
                continue
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            usage = payload.get("usage")
            if isinstance(usage, dict):
                return {"usage": usage}
    return None


def _streaming_response(
    upstream: httpx.Response,
    extra_headers: dict[str, str] | None = None,
    on_close: Callable[[dict[str, Any] | None], Awaitable[None]] | None = None,
) -> StreamingResponse:
    content_type = upstream.headers.get("content-type", "text/event-stream")
    headers = _response_headers(upstream, extra_headers)
    headers["content-type"] = content_type

    async def body_iterator() -> AsyncIterator[bytes]:
        tail: deque[bytes] = deque(maxlen=2)
        try:
            async for chunk in upstream.aiter_bytes():
                tail.append(chunk)
                yield chunk
        finally:
            payload: dict[str, Any] | None = None
            if tail and on_close is not None:
                try:
                    payload = _extract_usage_from_sse(b"".join(tail))
                except Exception:
                    payload = None
            try:
                if on_close is not None:
                    await on_close(payload)
            except Exception:
                logger.exception("streaming_usage_event_callback_failed")
            await upstream.aclose()

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

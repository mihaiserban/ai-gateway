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
from router.live_catalog import build_live_model_catalog
from router.metrics import Metrics
from router.redaction import redact_payload
from router.routing import DEFAULT_TIMEOUT_SECONDS, is_retryable_failure, resolve_model_request
from router.routing_state import GatewayRoutingState
from router.sessions import MemorySessionStore, RedisSessionStore, SessionStore
from router.usage_events import HttpUsageEventSink, UsageEvent, extract_usage, fingerprint

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
    app.state.routing_state = GatewayRoutingState(quota_cooldown_seconds=app.state.route_config.quota_cooldown_seconds)
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
        payload = app.state.metrics.snapshot()
        payload["routing_state"] = app.state.routing_state.snapshot()
        return JSONResponse(status_code=200, content=payload)

    @app.get("/v1/models")
    async def models(request: Request) -> Response:
        view = request.query_params.get("view", app.state.route_config.catalog_default_view)
        try:
            data = build_live_model_catalog(
                app.state.route_config,
                view=view,
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "type": "gateway_catalog_view_invalid",
                        "message": str(exc),
                        "view": view,
                    }
                },
            )
        return JSONResponse({"object": "list", "data": data})

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        start = time.perf_counter()
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(status_code=422, content={"error": "request body must be valid JSON"})
        if not isinstance(body, dict):
            return JSONResponse(status_code=422, content={"error": "request body must be a JSON object"})

        config = app.state.route_config
        state = app.state.routing_state
        now = time.time()
        auth = request.headers.get("authorization")
        x_session_id = request.headers.get("X-Session-Id")
        requested_model_raw = body.get("model")
        requested_model = requested_model_raw if isinstance(requested_model_raw, str) else None

        resolved = resolve_model_request(requested_model, config, state, now=now)

        if resolved.kind == "not-found":
            return _gateway_error(
                status_code=404,
                error_type="gateway_model_not_found",
                message=f"model {resolved.requested_model!r} is not configured",
                requested_model=resolved.requested_model,
                extra_headers=_gateway_headers(resolved, served="", attempted=[], fallback_count=0),
            )
        if resolved.kind == "unavailable":
            return _gateway_error(
                status_code=503,
                error_type="gateway_no_active_deployment",
                message=f"no active deployment for model {resolved.requested_model!r}",
                requested_model=resolved.requested_model,
                kind=_configured_model_kind(config, resolved.requested_model),
                inactive_reasons=_inactive_reasons(config, resolved.requested_model),
                extra_headers=_gateway_headers(resolved, served="", attempted=[], fallback_count=0),
            )

        upstream_body = redact_payload(body)
        is_stream = bool(body.get("stream"))

        # Warm-session stickiness only applies when an explicit X-Session-Id is
        # present. The fallback session id (prompt-derived) is used for usage
        # accounting only and must NOT pin a deployment.
        warm_deployment = await _resolve_warm_deployment(app, x_session_id, auth, resolved, state, now)
        ordered = list(resolved.ordered_deployments)
        if warm_deployment is not None and warm_deployment in ordered:
            ordered.remove(warm_deployment)
            ordered.insert(0, warm_deployment)

        session_id = x_session_id or _fallback_session_id(body, auth)
        session_key = _warm_session_key(x_session_id, auth)

        attempted: list[str] = []
        last_response: httpx.Response | None = None
        last_exception: Exception | None = None
        fallback_count = 0

        for index, deployment_id in enumerate(ordered):
            upstream_body["model"] = deployment_id
            if is_stream:
                upstream_body["stream_options"] = {
                    **(upstream_body.get("stream_options") or {}),
                    "include_usage": True,
                }
            attempted.append(deployment_id)
            attempt_token = state.start_attempt(deployment_id)
            attempt_started = time.perf_counter()
            attempt_status: int | str = "upstream_error"
            try:
                if is_stream:
                    response = await _proxy_stream(request, "/v1/chat/completions", upstream_body)
                else:
                    response = await _proxy_json(request, "/v1/chat/completions", upstream_body)
                attempt_status = response.status_code
            except httpx.TransportError as exc:
                last_exception = exc
                last_response = None
                attempt_status = "transport_error"
                app.state.metrics.record_provider_attempt(
                    deployment_id, _exception_status(exc), success=False, retryable_failure=True
                )
                fallback_count = index + 1
                continue
            finally:
                latency_ms = (time.perf_counter() - attempt_started) * 1000
                state.finish_attempt(attempt_token, status=attempt_status, latency_ms=latency_ms)
            last_response = response
            last_exception = None
            success = _is_success(response)
            should_fallback = not success and is_retryable_failure(response.status_code)
            app.state.metrics.record_provider_attempt(
                deployment_id,
                response.status_code,
                success=success,
                retryable_failure=should_fallback,
            )
            if success:
                break
            if is_stream and not success:
                # Read and close the failed stream attempt so its connection
                # returns to the pool, but keep last_response pointing at it
                # so the post-loop pass-through can return the upstream error
                # body with gateway headers.
                await response.aread()
                await response.aclose()
            if not should_fallback:
                break
            if index == len(ordered) - 1:
                break
            delay = min(config.retry_base_delay * (2**index), config.retry_max_delay)
            await app.state.async_sleep(delay)
            fallback_count = index + 1

        served_deployment = attempted[-1] if attempted else ""
        total_latency_ms = int((time.perf_counter() - start) * 1000)

        gateway_headers = _gateway_headers(
            resolved, served=served_deployment, attempted=attempted, fallback_count=fallback_count
        )

        if last_response is not None and _is_success(last_response):
            await app.state.session_store.set(
                session_key,
                {
                    "requested_model": resolved.requested_model,
                    "model_kind": resolved.kind,
                    "served_deployment": served_deployment,
                    "timestamp": time.time(),
                },
                ttl_seconds=config.cache_ttl_seconds,
            )

        status = last_response.status_code if last_response is not None else 502

        _log_request(
            session_id,
            served_deployment,
            resolved.requested_model,
            resolved.kind,
            status,
            total_latency_ms,
            fallback_count,
            attempted,
        )
        cache_hit = _cache_hit(last_response) if last_response is not None else None
        app.state.metrics.record(resolved.requested_model, served_deployment, fallback_count, cache_hit=cache_hit)

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
                requested_model=resolved.requested_model,
                selected_model=resolved.requested_model,
                served_model=served_deployment,
                provider_model=served_deployment,
                reason=resolved.kind,
                status=status,
                latency_ms=total_latency_ms,
                fallback_count=fallback_count,
                fallback_from=resolved.requested_model if fallback_count > 0 else None,
                cache_hit=cache_hit,
                payload=upstream_payload,
                error_class=_exception_class(last_exception),
                stream=is_stream,
            )

        if last_response is not None:
            if is_stream and _is_success(last_response):
                return _streaming_response(
                    last_response,
                    extra_headers=gateway_headers,
                    on_close=_make_usage_event_callback(
                        app,
                        request=request,
                        session_id=session_id,
                        requested_model=resolved.requested_model,
                        selected_model=resolved.requested_model,
                        served_model=served_deployment,
                        provider_model=served_deployment,
                        reason=resolved.kind,
                        status=status,
                        latency_ms=total_latency_ms,
                        fallback_count=fallback_count,
                        fallback_from=resolved.requested_model if fallback_count > 0 else None,
                        cache_hit=cache_hit,
                        error_class=_exception_class(last_exception),
                        stream=is_stream,
                    ),
                )
            # HTTP-failure exhaustion (including streaming that failed before
            # a stream started): pass through the last upstream error body
            # and status code with gateway headers attached.
            if is_stream:
                return Response(
                    content=last_response.content,
                    status_code=last_response.status_code,
                    media_type=last_response.headers.get("content-type", "application/json"),
                    headers=gateway_headers,
                )
            return _response_from_upstream(last_response, extra_headers=gateway_headers)

        # Transport-failure exhaustion: all candidates failed with transport
        # exceptions before a response stream started. Return 502 with the
        # mandated gateway_upstream_exhausted error shape.
        last_status = _exception_status(last_exception) if last_exception is not None else "transport_error"
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "type": "gateway_upstream_exhausted",
                    "message": "All candidate deployments failed before a response stream started.",
                    "model": resolved.requested_model,
                    "attempted": attempted,
                    "last_status": last_status,
                }
            },
            headers=gateway_headers,
        )

    @app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def unsupported_v1_path(path: str) -> JSONResponse:
        return JSONResponse(
            status_code=501,
            content={"error": f"/v1/{path} is not implemented by the sticky router"},
        )

    async def _proxy_json(
        request: Request,
        path: str,
        body: dict[str, Any],
    ) -> httpx.Response:
        url = app.state.litellm_base_url + path
        guard = app.state.upstream_semaphore or nullcontext()
        async with guard:
            return cast(
                httpx.Response,
                await app.state.http_client.post(
                    url, headers=_forward_headers(request), json=body, timeout=DEFAULT_TIMEOUT_SECONDS
                ),
            )

    async def _proxy_stream(
        request: Request,
        path: str,
        body: dict[str, Any],
    ) -> httpx.Response:
        url = app.state.litellm_base_url + path
        guard = app.state.upstream_semaphore or nullcontext()
        async with guard:
            req = app.state.http_client.build_request("POST", url, headers=_forward_headers(request), json=body)
            req.extensions = {
                "timeout": {
                    "connect": DEFAULT_TIMEOUT_SECONDS,
                    "read": DEFAULT_TIMEOUT_SECONDS,
                    "write": DEFAULT_TIMEOUT_SECONDS,
                    "pool": DEFAULT_TIMEOUT_SECONDS,
                }
            }
            return cast(httpx.Response, await app.state.http_client.send(req, stream=True))

    return app


def _is_success(response: httpx.Response) -> bool:
    return 200 <= response.status_code < 300


def _log_request(
    session_id: str,
    served_deployment: str,
    requested_model: str,
    reason: str,
    status: int | str,
    latency_ms: int,
    fallback_count: int,
    attempted: list[str],
) -> None:
    session_id_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
    parts = [
        f"session_id_hash={session_id_hash}",
        f"model={served_deployment}",
    ]
    parts.extend(
        [
            f"requested_model={requested_model}",
            f"reason={reason}",
            f"status={status}",
            f"latency_ms={latency_ms}",
            f"fallback_count={fallback_count}",
        ]
    )
    if fallback_count > 0 and attempted:
        parts.append(f"fallback_from={attempted[0]}")
    logger.info(" ".join(parts))


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
    fallback_from: str | None,
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
        estimated_cost_usd=None,
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


def _gateway_headers(
    resolved: Any,
    *,
    served: str,
    attempted: list[str],
    fallback_count: int,
) -> dict[str, str]:
    headers = {
        "X-Gateway-Requested-Model": resolved.requested_model,
        "X-Gateway-Model-Kind": resolved.kind,
        "X-Gateway-Served-Deployment": served,
        "X-Gateway-Fallback-Count": str(fallback_count),
        "X-Gateway-Attempted-Models": ",".join(attempted) if attempted else "",
    }
    if fallback_count > 0 and attempted:
        headers["X-Gateway-Fallback-From"] = attempted[0]
    return headers


def _gateway_error(
    *,
    status_code: int,
    error_type: str,
    message: str,
    requested_model: str,
    extra_headers: dict[str, str],
    kind: str | None = None,
    inactive_reasons: dict[str, list[str]] | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {"type": error_type, "message": message, "model": requested_model}
    if kind is not None:
        error["kind"] = kind
    if inactive_reasons is not None:
        error["inactive_reasons"] = inactive_reasons
    return JSONResponse(
        status_code=status_code,
        content={"error": error},
        headers=extra_headers,
    )


def _inactive_reasons(config: Any, requested_model: str) -> dict[str, list[str]]:
    from router.live_catalog import deployment_is_active

    env = os.environ
    reasons: dict[str, list[str]] = {}
    if requested_model in config.deployments:
        deployment_ids = [requested_model]
    else:
        deployment_ids = config.registry_models.get(requested_model, [])
    if not deployment_ids:
        combo = config.combos.get(requested_model)
        if combo is not None:
            deployment_ids = list(combo.candidates)
    for dep_id in deployment_ids:
        dep = config.deployments.get(dep_id)
        if dep is None:
            reasons[dep_id] = ["not_configured"]
            continue
        active, missing = deployment_is_active(dep, env)
        if not active:
            reasons[dep_id] = [f"missing env {name}" for name in missing]
    return reasons


def _configured_model_kind(config: Any, requested_model: str) -> str | None:
    if requested_model in config.combos:
        return "combo"
    if requested_model in config.deployments:
        return "connection-model"
    if requested_model in config.registry_models:
        return "registry-model"
    return None


async def _resolve_warm_deployment(
    app: FastAPI,
    x_session_id: str | None,
    auth: str | None,
    resolved: Any,
    state: GatewayRoutingState,
    now: float,
) -> str | None:
    if not x_session_id:
        return None
    session_key = _warm_session_key(x_session_id, auth)
    session = await app.state.session_store.get(session_key)
    if not session:
        return None
    served = session.get("served_deployment")
    if not isinstance(served, str) or served not in resolved.ordered_deployments:
        return None
    if state.in_quota_cooldown(served, now):
        return None
    return served


def _warm_session_key(x_session_id: str | None, auth: str | None) -> str:
    if not x_session_id:
        return ""
    auth_fp = hashlib.sha256((auth or "").encode("utf-8")).hexdigest() if auth else ""
    return hashlib.sha256(f"{auth_fp}:{x_session_id}".encode()).hexdigest()


app = create_app()

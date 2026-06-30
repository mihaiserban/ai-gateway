from __future__ import annotations

import socket
from typing import Any
from urllib.parse import urlparse

import httpx
import redis.asyncio as redis

HEALTH_TIMEOUT = 2.0


async def check_litellm(base_url: str, transport: httpx.AsyncBaseTransport | None) -> str:
    url = f"{base_url.rstrip('/')}/health/liveliness"
    try:
        async with httpx.AsyncClient(transport=transport, timeout=HEALTH_TIMEOUT) as client:
            response = await client.get(url)
        return "ok" if response.status_code == 200 else "degraded"
    except Exception:
        return "degraded"


async def check_redis(app_state: Any) -> str:
    redis_url = getattr(app_state, "redis_url", None)
    if not redis_url:
        return "disabled"
    client = getattr(app_state, "redis_client", None)
    if client is None:
        client = redis.from_url(redis_url, decode_responses=True)
        app_state.redis_client = client
    try:
        ok = await client.ping()
        return "ok" if ok else "degraded"
    except Exception:
        return "degraded"


def check_postgres(database_url: str | None) -> str:
    if not database_url:
        return "disabled"
    parsed = urlparse(database_url)
    host = parsed.hostname
    port = parsed.port or 5432
    if not host:
        return "degraded"
    try:
        with socket.create_connection((host, port), timeout=HEALTH_TIMEOUT):
            return "ok"
    except Exception:
        return "degraded"


async def gather_health(app_state: Any) -> dict[str, str]:
    litellm = await check_litellm(app_state.litellm_base_url, app_state.transport)
    redis_status = await check_redis(app_state)
    postgres_status = check_postgres(getattr(app_state, "database_url", None))

    statuses = {
        "router": "ok",
        "litellm": litellm,
        "redis": redis_status,
        "postgres": postgres_status,
    }
    statuses["status"] = "degraded" if any(v == "degraded" for v in statuses.values()) else "ok"
    return statuses


def all_ready(statuses: dict[str, str]) -> bool:
    return all(
        value == "ok" for key, value in statuses.items() if key not in ("status", "router") and value != "disabled"
    )

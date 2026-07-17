from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlparse

import redis.asyncio as redis

HEALTH_TIMEOUT = 2.0


async def check_litellm(app_state: Any) -> str:
    url = f"{app_state.litellm_base_url.rstrip('/')}/health/liveliness"
    try:
        response = await app_state.http_client.get(url, timeout=HEALTH_TIMEOUT)
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


async def check_postgres(database_url: str | None) -> str:
    if not database_url:
        return "disabled"
    try:
        parsed = urlparse(database_url)
        host = parsed.hostname
        port = parsed.port or 5432
        if not host:
            return "degraded"
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=HEALTH_TIMEOUT,
        )
        writer.close()
        await writer.wait_closed()
        return "ok"
    except Exception:
        return "degraded"


async def gather_health(app_state: Any) -> dict[str, str]:
    litellm, redis_status, postgres_status = await asyncio.gather(
        check_litellm(app_state),
        check_redis(app_state),
        check_postgres(getattr(app_state, "database_url", None)),
    )

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

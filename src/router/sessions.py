from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Protocol

import redis.asyncio as redis


class SessionStore(Protocol):
    async def get(self, session_id: str) -> dict[str, Any] | None: ...

    async def set(self, session_id: str, value: dict[str, Any], ttl_seconds: int) -> None: ...


class MemorySessionStore:
    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._sessions: dict[str, tuple[dict[str, Any], float]] = {}

    async def get(self, session_id: str) -> dict[str, Any] | None:
        entry = self._sessions.get(session_id)
        if entry is None:
            return None
        value, expires_at = entry
        if self._clock() >= expires_at:
            del self._sessions[session_id]
            return None
        return value

    async def set(self, session_id: str, value: dict[str, Any], ttl_seconds: int) -> None:
        self._sessions[session_id] = (value, self._clock() + ttl_seconds)


class RedisSessionStore:
    def __init__(self, redis_url: str) -> None:
        self._redis = redis.from_url(redis_url, decode_responses=True)

    async def get(self, session_id: str) -> dict[str, Any] | None:
        raw = await self._redis.get(_key(session_id))
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    async def set(self, session_id: str, value: dict[str, Any], ttl_seconds: int) -> None:
        await self._redis.set(_key(session_id), json.dumps(value), ex=ttl_seconds)

    async def aclose(self) -> None:
        await self._redis.aclose()


def _key(session_id: str) -> str:
    return f"session:{session_id}"

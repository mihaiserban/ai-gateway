from __future__ import annotations

import json
from typing import Any, Protocol

import redis.asyncio as redis


class SessionStore(Protocol):
    async def get(self, session_id: str) -> dict[str, Any] | None:
        ...

    async def set(self, session_id: str, value: dict[str, Any], ttl_seconds: int) -> None:
        ...


class MemorySessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}

    async def get(self, session_id: str) -> dict[str, Any] | None:
        return self._sessions.get(session_id)

    async def set(self, session_id: str, value: dict[str, Any], ttl_seconds: int) -> None:
        self._sessions[session_id] = value


class RedisSessionStore:
    def __init__(self, redis_url: str) -> None:
        self._redis = redis.from_url(redis_url, decode_responses=True)

    async def get(self, session_id: str) -> dict[str, Any] | None:
        raw = await self._redis.get(_key(session_id))
        if raw is None:
            return None
        value = json.loads(raw)
        return value if isinstance(value, dict) else None

    async def set(self, session_id: str, value: dict[str, Any], ttl_seconds: int) -> None:
        await self._redis.set(_key(session_id), json.dumps(value), ex=ttl_seconds)


def _key(session_id: str) -> str:
    return f"session:{session_id}"

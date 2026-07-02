from __future__ import annotations

from typing import Any

import redis.asyncio as redis


class RedisStatsCollector:
    """Live Redis INFO sampler for the dashboard.

    Collects only safe, aggregate statistics; never enumerates keys or reads
    values. Returns an empty/disabled payload when Redis is not configured.
    """

    def __init__(self, redis_url: str | None) -> None:
        self.redis_url = redis_url
        self._client: redis.Redis | None = None

    async def snapshot(self) -> dict[str, Any]:
        if not self.redis_url:
            return {"enabled": False}

        if self._client is None:
            self._client = redis.from_url(self.redis_url, decode_responses=True)

        try:
            info = await self._client.info()
        except Exception as exc:
            return {"enabled": True, "error": f"redis unavailable: {type(exc).__name__}"}

        return {
            "enabled": True,
            "memory": {
                "used_bytes": info.get("used_memory"),
                "used_human": info.get("used_memory_human"),
                "peak_human": info.get("used_memory_peak_human"),
                "maxmemory_human": info.get("maxmemory_human"),
            },
            "commands": {
                "total_processed": info.get("total_commands_processed"),
            },
            "keyspace": {
                "hits": info.get("keyspace_hits"),
                "misses": info.get("keyspace_misses"),
            },
            "clients": {
                "connected": info.get("connected_clients"),
                "blocked": info.get("blocked_clients"),
            },
            "expired_keys": info.get("expired_keys"),
            "evicted_keys": info.get("evicted_keys"),
            "db_keys": _sum_db_keys(info),
        }

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _sum_db_keys(info: dict[str, Any]) -> int | None:
    total = 0
    found = False
    for key, value in info.items():
        if key.startswith("db") and isinstance(value, dict) and "keys" in value:
            total += int(value["keys"])
            found = True
    return total if found else None

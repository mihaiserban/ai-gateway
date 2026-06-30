from __future__ import annotations

import pytest

from router.redis_stats import RedisStatsCollector, _sum_db_keys


@pytest.mark.asyncio
async def test_collector_returns_disabled_payload_without_url():
    collector = RedisStatsCollector(None)
    assert await collector.snapshot() == {"enabled": False}


def test_sum_db_keys_adds_keys_per_db():
    info = {
        "db0": {"keys": 12, "expires": 3, "avg_ttl": 600},
        "db1": {"keys": 5, "expires": 1, "avg_ttl": 300},
    }
    assert _sum_db_keys(info) == 17


def test_sum_db_keys_returns_none_when_no_db_sections():
    assert _sum_db_keys({"used_memory": 123}) is None


@pytest.mark.asyncio
async def test_collector_returns_error_on_connection_failure():
    class BrokenRedis:
        async def info(self) -> dict[str, object]:
            raise ConnectionError("boom")

    collector = RedisStatsCollector("redis://localhost:6379")
    collector._client = BrokenRedis()  # type: ignore[assignment]

    snapshot = await collector.snapshot()

    assert snapshot["enabled"] is True
    assert "redis unavailable" in snapshot["error"]


@pytest.mark.asyncio
async def test_collector_parses_info_into_snapshot():
    class FakeRedis:
        async def info(self) -> dict[str, object]:
            return {
                "used_memory": 1234567,
                "used_memory_human": "1.18M",
                "used_memory_peak_human": "1.50M",
                "maxmemory_human": "0B",
                "total_commands_processed": 999,
                "keyspace_hits": 800,
                "keyspace_misses": 200,
                "connected_clients": 7,
                "blocked_clients": 1,
                "expired_keys": 42,
                "evicted_keys": 3,
                "db0": {"keys": 10, "expires": 2, "avg_ttl": 100},
                "db1": {"keys": 5, "expires": 1, "avg_ttl": 200},
            }

    collector = RedisStatsCollector("redis://localhost:6379")
    collector._client = FakeRedis()  # type: ignore[assignment]

    snapshot = await collector.snapshot()

    assert snapshot["enabled"] is True
    assert snapshot["memory"]["used_bytes"] == 1234567
    assert snapshot["memory"]["used_human"] == "1.18M"
    assert snapshot["commands"]["total_processed"] == 999
    assert snapshot["keyspace"]["hits"] == 800
    assert snapshot["keyspace"]["misses"] == 200
    assert snapshot["clients"]["connected"] == 7
    assert snapshot["clients"]["blocked"] == 1
    assert snapshot["expired_keys"] == 42
    assert snapshot["evicted_keys"] == 3
    assert snapshot["db_keys"] == 15

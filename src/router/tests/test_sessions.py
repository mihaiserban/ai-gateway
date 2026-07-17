import json

import pytest

from router.sessions import MemorySessionStore, RedisSessionStore


class FakeRedis:
    def __init__(self, raw=None):
        self.raw = raw
        self.set_calls = []
        self.closed = False

    async def get(self, key):
        return self.raw

    async def set(self, *args, **kwargs):
        self.set_calls.append((args, kwargs))

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_memory_session_store_round_trips_session():
    store = MemorySessionStore()

    await store.set("abc", {"model": "explorer"}, ttl_seconds=600)

    assert await store.get("abc") == {"model": "explorer"}


@pytest.mark.asyncio
async def test_memory_session_store_returns_none_for_missing_session():
    store = MemorySessionStore()

    assert await store.get("missing") is None


@pytest.mark.asyncio
async def test_memory_session_store_expires_sessions_after_ttl():
    now = 1_000.0
    store = MemorySessionStore(clock=lambda: now)

    await store.set("abc", {"model": "explorer"}, ttl_seconds=10)

    now = 1_010.0

    assert await store.get("abc") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("[]", None),
        ('{"model":"coder"}', {"model": "coder"}),
        ("not-json", None),
    ],
)
async def test_redis_session_store_treats_missing_non_mapping_and_corrupt_state_as_misses(monkeypatch, raw, expected):
    fake = FakeRedis(raw)
    monkeypatch.setattr("router.sessions.redis.from_url", lambda *_args, **_kwargs: fake)
    store = RedisSessionStore("redis://cache")

    assert await store.get("session-id") == expected


@pytest.mark.asyncio
async def test_redis_session_store_serializes_ttl_and_closes(monkeypatch):
    fake = FakeRedis()

    def from_url(url, *, decode_responses):
        assert (url, decode_responses) == ("redis://cache", True)
        return fake

    monkeypatch.setattr("router.sessions.redis.from_url", from_url)
    store = RedisSessionStore("redis://cache")

    await store.set("session-id", {"model": "coder"}, ttl_seconds=600)
    await store.aclose()

    args, kwargs = fake.set_calls[0]
    assert args[0] == "session:session-id"
    assert json.loads(args[1]) == {"model": "coder"}
    assert kwargs == {"ex": 600}
    assert fake.closed is True

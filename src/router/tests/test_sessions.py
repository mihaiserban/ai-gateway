import pytest

from router.sessions import MemorySessionStore


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

    now = 1_011.0

    assert await store.get("abc") is None

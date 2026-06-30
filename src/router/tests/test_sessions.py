import pytest

from router.sessions import MemorySessionStore


@pytest.mark.asyncio
async def test_memory_session_store_round_trips_session():
    store = MemorySessionStore()

    await store.set("abc", {"model": "fast"}, ttl_seconds=600)

    assert await store.get("abc") == {"model": "fast"}


@pytest.mark.asyncio
async def test_memory_session_store_returns_none_for_missing_session():
    store = MemorySessionStore()

    assert await store.get("missing") is None

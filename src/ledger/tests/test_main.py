import json
import types

import httpx
import pytest

import ledger.main as ledger_main
from ledger.main import UsageEvent, create_app


class FakeRepository:
    def __init__(self) -> None:
        self.events: list[UsageEvent] = []

    def record(self, event: UsageEvent) -> None:
        self.events.append(event)


def _event() -> UsageEvent:
    return UsageEvent(
        timestamp=1782820800.0,
        path="/v1/chat/completions",
        method="POST",
        key_hash="keyhash",
        session_hash="sessionhash",
        requested_model="coder",
        selected_model="coder",
        served_model="coder",
        provider_model="ollama_chat/kimi-k2.7-code",
        reason="explicit-model",
        status="200",
        latency_ms=123,
        prompt_tokens=10,
        completion_tokens=4,
        total_tokens=14,
        estimated_cost_usd=5.5,
        cache_status="hit",
        fallback_count=0,
        fallback_from=None,
        error_class=None,
        stream=False,
    )


@pytest.mark.asyncio
async def test_post_usage_event_returns_202_and_writes_row():
    repository = FakeRepository()
    app = create_app(repository=repository)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/usage-events", json=_event().model_dump())

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    assert len(repository.events) == 1
    stored = repository.events[0]
    assert stored.served_model == "coder"
    assert stored.prompt_tokens == 10
    assert stored.estimated_cost_usd == 5.5


@pytest.mark.asyncio
async def test_post_usage_event_records_in_worker_thread(monkeypatch: pytest.MonkeyPatch):
    repository = FakeRepository()
    calls = []

    async def fake_to_thread(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(ledger_main, "asyncio", types.SimpleNamespace(to_thread=fake_to_thread), raising=False)
    app = create_app(repository=repository)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/usage-events", json=_event().model_dump())

    assert response.status_code == 202
    assert calls == [(repository.record, (repository.events[0],), {})]


@pytest.mark.asyncio
async def test_post_usage_event_rejects_extra_prompt_body_field():
    app = create_app(repository=FakeRepository())

    payload = _event().model_dump()
    payload["prompt"] = "secret prompt text"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/usage-events", json=payload)

    assert response.status_code == 422
    assert "prompt" in json.dumps(response.json())


@pytest.mark.asyncio
async def test_healthz_returns_ok():
    app = create_app(repository=FakeRepository())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

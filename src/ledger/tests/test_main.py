import json
import math
import types

import httpx
import pytest
from pydantic import ValidationError

import ledger.main as ledger_main
from ledger.main import POSTGRES_INTEGER_MAX, SCHEMA, PostgresUsageRepository, UsageEvent, create_app


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
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timestamp", -1),
        ("latency_ms", -1),
        ("prompt_tokens", -1),
        ("completion_tokens", -1),
        ("total_tokens", -1),
        ("estimated_cost_usd", -0.01),
        ("fallback_count", -1),
        ("latency_ms", POSTGRES_INTEGER_MAX + 1),
        ("prompt_tokens", POSTGRES_INTEGER_MAX + 1),
        ("completion_tokens", POSTGRES_INTEGER_MAX + 1),
        ("total_tokens", POSTGRES_INTEGER_MAX + 1),
        ("fallback_count", POSTGRES_INTEGER_MAX + 1),
    ],
)
async def test_post_usage_event_rejects_invalid_numeric_boundaries(field, value):
    repository = FakeRepository()
    app = create_app(repository=repository)
    payload = _event().model_dump()
    payload[field] = value

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/usage-events", json=payload)

    assert response.status_code == 422
    assert repository.events == []


def test_usage_event_accepts_exact_postgres_integer_limit():
    payload = _event().model_dump()
    payload["latency_ms"] = POSTGRES_INTEGER_MAX

    assert UsageEvent.model_validate(payload).latency_ms == POSTGRES_INTEGER_MAX


@pytest.mark.parametrize("field", ["timestamp", "estimated_cost_usd"])
def test_usage_event_rejects_non_finite_numbers(field):
    payload = _event().model_dump()
    payload[field] = math.inf

    with pytest.raises(ValidationError):
        UsageEvent.model_validate(payload)


def test_postgres_repository_is_disabled_without_database_url(monkeypatch):
    monkeypatch.setattr(ledger_main.psycopg, "connect", lambda *_args, **_kwargs: pytest.fail("unexpected connect"))

    PostgresUsageRepository(None).record(_event())


def test_postgres_repository_executes_schema_and_parameterized_insert(monkeypatch):
    class Cursor:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement, params=None):
            self.calls.append((statement, params))

    class Connection:
        def __init__(self, cursor):
            self._cursor = cursor

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return self._cursor

    cursor = Cursor()
    monkeypatch.setattr(ledger_main.psycopg, "connect", lambda url: Connection(cursor))
    event = _event()

    PostgresUsageRepository("postgresql://db/gateway").record(event)

    assert cursor.calls[0] == (SCHEMA, None)
    insert, params = cursor.calls[1]
    assert "insert into gateway_usage_events" in insert
    assert params == tuple(event.model_dump().values())


@pytest.mark.asyncio
async def test_healthz_returns_ok():
    app = create_app(repository=FakeRepository())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

# Usage Ledger And Compatibility Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a prompt-free persistent usage ledger behind a dedicated ledger layer, and lock the router's supported OpenAI-compatible paths with SDK/path compatibility tests.

**Architecture:** The router does not import a Postgres driver and does not write to Postgres directly. It builds a prompt-free `UsageEvent` and sends it to a ledger interface; in Docker that interface is a small internal `usage-ledger` FastAPI service. The `usage-ledger` service is the only new gateway-owned component that writes `gateway_usage_events` to the existing Postgres database.

**Tech Stack:** FastAPI, httpx, pytest, pytest-asyncio, existing Postgres, `psycopg[binary]` only in the ledger service, Docker Compose.

## Global Constraints

- Do not add SQLite or a second persistence engine.
- Reuse the existing Postgres service and `DATABASE_URL`.
- The router must not depend on `psycopg`, `asyncpg`, SQLAlchemy, or Postgres SQL.
- Store only metadata. Do not store prompt bodies, response bodies, raw bearer tokens, or raw session IDs.
- The ledger service owns table `gateway_usage_events`; it must not depend on LiteLLM private tables.
- Router-to-ledger writes are best-effort and must not change chat request status codes.
- Keep `/metrics` as the in-memory debug endpoint; the ledger is the persistent metadata store.
- Preserve the router's current OpenAI-compatible surface unless a task explicitly adds a path.
- Use TDD for each task: write the failing test first, run it, implement the smallest change, run focused tests, then commit.
- Respect the existing dirty worktree. Do not revert unrelated changes.
- After implementation, invoke the `ponytail-review` skill on the resulting diff before declaring completion.

---

## File Structure

- Create `src/router/usage_events.py`: router-side `UsageEvent`, hashing, usage extraction, cost estimation, and `HttpUsageEventSink`.
- Create `src/router/tests/test_usage_events.py`: privacy, token extraction, cost, and HTTP sink tests.
- Modify `src/router/main.py`: initialize an injectable usage sink and emit one event per chat result.
- Create `src/router/tests/test_usage_event_integration.py`: FastAPI tests proving chat emits prompt-free events and sink failures are non-fatal.
- Modify `src/router/routing.py`: add `model_prices: dict[str, tuple[float, float]]` to `RouteConfig`.
- Modify `src/router/config.py`: load `model_prices` from generated router config.
- Modify `src/scripts/generate_configs.py`: render per-alias pricing metadata into `router_config.yaml`.
- Modify `src/router/router_config.yaml`: regenerated output containing `model_prices`.
- Create `src/ledger/Dockerfile`, `src/ledger/requirements.txt`, `src/ledger/main.py`, and `src/ledger/tests/test_main.py`: internal service that validates events and persists them to Postgres.
- Create `src/router/tests/test_compatibility_paths.py`: compatibility matrix tests for supported and unsupported paths.
- Modify `src/docker-compose.yml`: add internal `usage-ledger` service and point `sticky-router` at it with `USAGE_LEDGER_URL`.
- Modify `README.md` and `src/README.md`: document the ledger layer, privacy posture, query examples, and compatibility matrix.
- Modify `docs/TODO.md`: mark persistent usage ledger and SDK/path compatibility as active/completed according to the final result.

---

### Task 1: Route Config Carries Pricing Metadata

**Files:**
- Modify: `src/router/routing.py`
- Modify: `src/router/config.py`
- Modify: `src/scripts/generate_configs.py`
- Test: `src/router/tests/test_config.py`
- Test: `src/router/tests/test_gateway_config_generator.py`

**Interfaces:**
- Consumes: `model_info.input_cost_per_token` and `model_info.output_cost_per_token` from `src/gateway.config.yaml`.
- Produces: `RouteConfig.model_prices: dict[str, tuple[float, float]]`, keyed by router alias. Tuple order is `(input_cost_per_token, output_cost_per_token)`.

- [ ] **Step 1: Add failing generator test**

Add this test to `src/router/tests/test_gateway_config_generator.py`:

```python
def test_render_router_config_includes_resolved_model_prices():
    config = {
        "models": [
            {
                "name": "paid-provider",
                "litellm_model": "deepseek/example",
                "api_key_env": "DEEPSEEK_API_KEY",
                "model_info": {
                    "input_cost_per_token": 0.25,
                    "output_cost_per_token": 0.75,
                    "reasoning_level": "medium",
                },
            }
        ],
        "aliases": [{"name": "coder", "target": "paid-provider"}],
    }

    rendered = render_router_config(config)

    assert rendered["model_prices"] == {
        "paid-provider": {
            "input_cost_per_token": 0.25,
            "output_cost_per_token": 0.75,
        },
        "coder": {
            "input_cost_per_token": 0.25,
            "output_cost_per_token": 0.75,
        },
    }
```

- [ ] **Step 2: Implement config generation and loading**

In `src/scripts/generate_configs.py`, add `"model_prices"` to `render_router_config()`:

```python
"model_prices": {
    entry["name"]: price
    for entry in entries
    if (price := _price_info(_resolve_entry(entry, entries_by_name))) is not None
},
```

Add:

```python
def _price_info(entry: dict[str, Any]) -> dict[str, float] | None:
    model_info = entry.get("model_info") or {}
    input_cost = model_info.get("input_cost_per_token")
    output_cost = model_info.get("output_cost_per_token")
    if input_cost is None or output_cost is None:
        return None
    return {
        "input_cost_per_token": float(input_cost),
        "output_cost_per_token": float(output_cost),
    }
```

In `src/router/routing.py`, add to `RouteConfig`:

```python
model_prices: dict[str, tuple[float, float]] = field(default_factory=dict)
```

In `src/router/config.py`, add:

```python
def _model_prices(raw: Any) -> dict[str, tuple[float, float]]:
    if not isinstance(raw, dict):
        return {}
    prices: dict[str, tuple[float, float]] = {}
    for alias, value in raw.items():
        if not isinstance(alias, str) or not isinstance(value, dict):
            continue
        input_cost = value.get("input_cost_per_token")
        output_cost = value.get("output_cost_per_token")
        if input_cost is None or output_cost is None:
            continue
        prices[alias] = (float(input_cost), float(output_cost))
    return prices
```

Then parse and pass it in `_route_config_from_dict()`:

```python
model_prices = _model_prices(data.get("model_prices") or {})
```

```python
model_prices=model_prices,
```

- [ ] **Step 3: Add route config loader test**

Add this test to `src/router/tests/test_config.py`:

```python
def test_load_route_config_reads_model_prices(tmp_path):
    path = tmp_path / "router_config.yaml"
    path.write_text(
        """
cache_ttl_seconds: 600
default_model: coder
allowed_models:
  - coder
fallbacks:
  coder: []
model_prices:
  coder:
    input_cost_per_token: 0.25
    output_cost_per_token: 0.75
""".lstrip(),
        encoding="utf-8",
    )

    config = load_route_config(str(path))

    assert config.model_prices == {"coder": (0.25, 0.75)}
```

- [ ] **Step 4: Verify Task 1**

Run:

```bash
PYTHONPATH=src python3 -m pytest src/router/tests/test_gateway_config_generator.py::test_render_router_config_includes_resolved_model_prices src/router/tests/test_config.py::test_load_route_config_reads_model_prices -q
python3 src/scripts/generate_configs.py
```

Expected: tests PASS, and `src/router/router_config.yaml` contains `model_prices`.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/router/routing.py src/router/config.py src/scripts/generate_configs.py src/router/router_config.yaml src/router/tests/test_config.py src/router/tests/test_gateway_config_generator.py
git commit -m "feat: expose model pricing to router config"
```

---

### Task 2: Router Usage Event Builder And Sink

**Files:**
- Create: `src/router/usage_events.py`
- Create: `src/router/tests/test_usage_events.py`

**Interfaces:**
- Consumes: `RouteConfig.model_prices`.
- Produces:
  - `UsageEvent` dataclass with only prompt-free fields.
  - `fingerprint(value: str | None, length: int = 16) -> str`.
  - `extract_usage(payload: dict[str, Any] | None) -> tuple[int | None, int | None, int | None]`.
  - `estimate_cost_usd(model: str, prompt_tokens: int | None, completion_tokens: int | None, prices: dict[str, tuple[float, float]]) -> float | None`.
  - `HttpUsageEventSink(base_url: str | None, transport: httpx.AsyncBaseTransport | None = None)`.
  - `HttpUsageEventSink.record(event: UsageEvent) -> None`.

- [ ] **Step 1: Write failing tests**

Create `src/router/tests/test_usage_events.py`:

```python
import httpx
import pytest

from router.usage_events import HttpUsageEventSink, UsageEvent, estimate_cost_usd, extract_usage, fingerprint


def test_fingerprint_never_returns_raw_secret():
    result = fingerprint("Bearer sk-secret-value")
    assert result != "Bearer sk-secret-value"
    assert "sk-secret" not in result
    assert len(result) == 16


def test_extract_usage_handles_openai_usage_shape():
    assert extract_usage({"usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}) == (11, 7, 18)


def test_extract_usage_returns_none_tuple_when_missing():
    assert extract_usage({}) == (None, None, None)
    assert extract_usage(None) == (None, None, None)


def test_estimate_cost_usd_uses_per_token_prices():
    assert estimate_cost_usd("coder", 10, 4, {"coder": (0.25, 0.75)}) == 5.5


@pytest.mark.asyncio
async def test_disabled_http_usage_sink_is_noop():
    sink = HttpUsageEventSink(None)
    await sink.record(_event())


@pytest.mark.asyncio
async def test_http_usage_sink_posts_prompt_free_event():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = request.read().decode()
        return httpx.Response(202, json={"status": "accepted"})

    sink = HttpUsageEventSink("http://usage-ledger:4200", transport=httpx.MockTransport(handler))

    await sink.record(_event())

    assert seen["url"] == "http://usage-ledger:4200/usage-events"
    assert "secret prompt" not in seen["json"]
    assert "sk-secret" not in seen["json"]
    assert "raw-session" not in seen["json"]
    assert "keyhash" in seen["json"]
    assert "sessionhash" in seen["json"]


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
```

- [ ] **Step 2: Implement router usage event module**

Create `src/router/usage_events.py`:

```python
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class UsageEvent:
    timestamp: float
    path: str
    method: str
    key_hash: str
    session_hash: str
    requested_model: str | None
    selected_model: str
    served_model: str
    provider_model: str
    reason: str
    status: str
    latency_ms: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    estimated_cost_usd: float | None
    cache_status: str
    fallback_count: int
    fallback_from: str | None
    error_class: str | None
    stream: bool


class HttpUsageEventSink:
    def __init__(
        self,
        base_url: str | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") if base_url else None
        self.transport = transport

    async def record(self, event: UsageEvent) -> None:
        if not self.base_url:
            return
        async with httpx.AsyncClient(transport=self.transport, timeout=2.0) as client:
            response = await client.post(f"{self.base_url}/usage-events", json=asdict(event))
            response.raise_for_status()


def fingerprint(value: str | None, length: int = 16) -> str:
    if not value:
        return "anonymous"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def extract_usage(payload: dict[str, Any] | None) -> tuple[int | None, int | None, int | None]:
    if not isinstance(payload, dict):
        return (None, None, None)
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return (None, None, None)
    return (
        _optional_int(usage.get("prompt_tokens")),
        _optional_int(usage.get("completion_tokens")),
        _optional_int(usage.get("total_tokens")),
    )


def estimate_cost_usd(
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    prices: dict[str, tuple[float, float]],
) -> float | None:
    if prompt_tokens is None or completion_tokens is None:
        return None
    price = prices.get(model)
    if price is None:
        return None
    input_cost, output_cost = price
    return prompt_tokens * input_cost + completion_tokens * output_cost


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None
```

- [ ] **Step 3: Verify Task 2**

Run:

```bash
PYTHONPATH=src python3 -m pytest src/router/tests/test_usage_events.py -q
python3 -m ruff check src/router/usage_events.py src/router/tests/test_usage_events.py
python3 -m mypy src/router/usage_events.py
```

Expected: all PASS.

- [ ] **Step 4: Commit Task 2**

```bash
git add src/router/usage_events.py src/router/tests/test_usage_events.py
git commit -m "feat: add router usage event sink"
```

---

### Task 3: Router Emits Usage Events Without DB Access

**Files:**
- Modify: `src/router/main.py`
- Create: `src/router/tests/test_usage_event_integration.py`

**Interfaces:**
- Consumes: `UsageEvent`, `HttpUsageEventSink`, `extract_usage()`, `estimate_cost_usd()`, and `fingerprint()` from Task 2.
- Produces: best-effort event emission for every `/v1/chat/completions` request.

- [ ] **Step 1: Write failing integration tests**

Create `src/router/tests/test_usage_event_integration.py`:

```python
import json

import httpx
import pytest

from router.main import create_app
from router.usage_events import UsageEvent


class FakeSink:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.events: list[UsageEvent] = []

    async def record(self, event: UsageEvent) -> None:
        if self.fail:
            raise RuntimeError("usage sink unavailable")
        self.events.append(event)


@pytest.mark.asyncio
async def test_chat_emits_prompt_free_usage_event():
    sink = FakeSink()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            },
            headers={"x-litellm-cache-hit": "true"},
        )

    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=httpx.MockTransport(handler),
        usage_sink=sink,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-test-secret", "X-Session-Id": "session-raw"},
            json={
                "model": "coder",
                "messages": [{"role": "user", "content": "secret prompt text must not be stored"}],
            },
        )

    assert response.status_code == 200
    event = sink.events[0]
    assert event.key_hash != "Bearer sk-test-secret"
    assert event.session_hash != "session-raw"
    assert event.requested_model == "coder"
    assert event.selected_model == "coder"
    assert event.served_model == "coder"
    assert event.status == "200"
    assert event.prompt_tokens == 10
    assert event.completion_tokens == 4
    assert event.total_tokens == 14
    assert event.cache_status == "hit"
    assert event.stream is False
    rendered = json.dumps(event.__dict__, default=str)
    assert "secret prompt text" not in rendered
    assert "sk-test-secret" not in rendered
    assert "session-raw" not in rendered


@pytest.mark.asyncio
async def test_usage_sink_failure_does_not_fail_chat():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        transport=httpx.MockTransport(handler),
        usage_sink=FakeSink(fail=True),
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "sink-failure"},
            json={"messages": [{"role": "user", "content": "say hello"}]},
        )

    assert response.status_code == 200
```

- [ ] **Step 2: Implement event emission**

In `src/router/main.py`, import:

```python
from router.usage_events import HttpUsageEventSink, UsageEvent, estimate_cost_usd, extract_usage, fingerprint
```

Update `create_app()`:

```python
usage_sink: Any | None = None,
```

After `app.state.metrics = Metrics()`, initialize:

```python
app.state.usage_sink = usage_sink or HttpUsageEventSink(os.environ.get("USAGE_LEDGER_URL"))
```

Add helper:

```python
async def _record_usage_event(
    app: FastAPI,
    *,
    request: Request,
    session_id: str,
    requested_model: str | None,
    selected_model: str,
    served_model: str,
    provider_model: str,
    reason: str,
    status: int | str,
    latency_ms: int,
    fallback_count: int,
    fallback_from: str,
    cache_hit: bool | None,
    upstream_payload: dict[str, Any] | None,
    error_class: str | None,
    stream: bool,
) -> None:
    prompt_tokens, completion_tokens, total_tokens = extract_usage(upstream_payload)
    event = UsageEvent(
        timestamp=time.time(),
        path=str(request.url.path),
        method=request.method,
        key_hash=fingerprint(request.headers.get("authorization")),
        session_hash=fingerprint(session_id),
        requested_model=requested_model,
        selected_model=selected_model,
        served_model=served_model,
        provider_model=provider_model,
        reason=reason,
        status=str(status),
        latency_ms=latency_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        estimated_cost_usd=estimate_cost_usd(served_model, prompt_tokens, completion_tokens, app.state.route_config.model_prices),
        cache_status=_cache_status(cache_hit),
        fallback_count=fallback_count,
        fallback_from=fallback_from if fallback_count > 0 else None,
        error_class=error_class,
        stream=stream,
    )
    try:
        await app.state.usage_sink.record(event)
    except Exception:
        logger.exception("usage_event_sink_failed")
```

Add:

```python
def _cache_status(cache_hit: bool | None) -> str:
    if cache_hit is True:
        return "hit"
    if cache_hit is False:
        return "miss"
    return "unknown"
```

Call `_record_usage_event(...)` once after final `status` and `cache_hit` are known, before returning the response.

- [ ] **Step 3: Verify Task 3**

Run:

```bash
PYTHONPATH=src python3 -m pytest src/router/tests/test_usage_events.py src/router/tests/test_usage_event_integration.py src/router/tests/test_metrics.py src/router/tests/test_streaming.py -q
python3 -m ruff check src/router/main.py src/router/usage_events.py src/router/tests/test_usage_event_integration.py
python3 -m mypy src/router
```

Expected: all PASS.

- [ ] **Step 4: Commit Task 3**

```bash
git add src/router/main.py src/router/tests/test_usage_event_integration.py
git commit -m "feat: emit usage events from router"
```

---

### Task 4: Dedicated Usage Ledger Service

**Files:**
- Create: `src/ledger/Dockerfile`
- Create: `src/ledger/requirements.txt`
- Create: `src/ledger/main.py`
- Create: `src/ledger/tests/test_main.py`

**Interfaces:**
- Consumes: HTTP `POST /usage-events` from the router.
- Produces: Postgres rows in `gateway_usage_events`.

- [ ] **Step 1: Create ledger service tests**

Create `src/ledger/tests/test_main.py` with tests for:

```python
def test_post_usage_event_returns_202_and_writes_row():
    # Use a fake repository object injected into app.state.repository.
    # Assert the endpoint returns 202 and receives the validated event.

def test_post_usage_event_rejects_extra_prompt_body_field():
    # POST a valid payload plus {"prompt": "secret"}.
    # Assert HTTP 422.
```

Use `httpx.ASGITransport` like the router tests. The fake repository should expose `record(event: UsageEvent) -> None`.

- [ ] **Step 2: Implement ledger service**

Create `src/ledger/main.py` with:

```python
from __future__ import annotations

import os
from typing import Any

import psycopg
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict
from starlette.responses import JSONResponse


SCHEMA = """
create table if not exists gateway_usage_events (
    id bigserial primary key,
    timestamp double precision not null,
    path text not null,
    method text not null,
    key_hash text not null,
    session_hash text not null,
    requested_model text,
    selected_model text not null,
    served_model text not null,
    provider_model text not null,
    reason text not null,
    status text not null,
    latency_ms integer not null,
    prompt_tokens integer,
    completion_tokens integer,
    total_tokens integer,
    estimated_cost_usd double precision,
    cache_status text not null,
    fallback_count integer not null,
    fallback_from text,
    error_class text,
    stream boolean not null
);
create index if not exists gateway_usage_events_timestamp_idx on gateway_usage_events(timestamp);
create index if not exists gateway_usage_events_key_hash_idx on gateway_usage_events(key_hash);
create index if not exists gateway_usage_events_served_model_idx on gateway_usage_events(served_model);
"""


class UsageEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: float
    path: str
    method: str
    key_hash: str
    session_hash: str
    requested_model: str | None = None
    selected_model: str
    served_model: str
    provider_model: str
    reason: str
    status: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    estimated_cost_usd: float | None = None
    cache_status: str
    fallback_count: int
    fallback_from: str | None = None
    error_class: str | None = None
    stream: bool


class PostgresUsageRepository:
    def __init__(self, database_url: str | None) -> None:
        self.database_url = database_url

    def record(self, event: UsageEvent) -> None:
        if not self.database_url:
            return
        with psycopg.connect(self.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA)
                cur.execute(
                    """
                    insert into gateway_usage_events (
                        timestamp, path, method, key_hash, session_hash, requested_model,
                        selected_model, served_model, provider_model, reason, status,
                        latency_ms, prompt_tokens, completion_tokens, total_tokens,
                        estimated_cost_usd, cache_status, fallback_count, fallback_from,
                        error_class, stream
                    )
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    tuple(event.model_dump().values()),
                )


def create_app(repository: Any | None = None) -> FastAPI:
    app = FastAPI(title="AI Gateway Usage Ledger")
    app.state.repository = repository or PostgresUsageRepository(os.environ.get("DATABASE_URL"))

    @app.post("/usage-events")
    async def usage_events(event: UsageEvent) -> JSONResponse:
        app.state.repository.record(event)
        return JSONResponse(status_code=202, content={"status": "accepted"})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
```

- [ ] **Step 3: Add ledger service runtime files**

Create `src/ledger/requirements.txt`:

```text
fastapi==0.138.2
uvicorn[standard]==0.34.0
psycopg[binary]==3.3.2
pydantic==2.12.5
```

Create `src/ledger/Dockerfile`:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app/ledger

ENV PYTHONPATH=/app
EXPOSE 4200

CMD ["uvicorn", "ledger.main:app", "--host", "0.0.0.0", "--port", "4200"]
```

- [ ] **Step 4: Verify Task 4**

Run:

```bash
PYTHONPATH=src python3 -m pytest src/ledger/tests -q
python3 -m ruff check src/ledger
```

Expected: all PASS.

- [ ] **Step 5: Commit Task 4**

```bash
git add src/ledger
git commit -m "feat: add usage ledger service"
```

---

### Task 5: Docker Wiring And Operations Docs

**Files:**
- Modify: `src/docker-compose.yml`
- Modify: `README.md`
- Modify: `src/README.md`
- Modify: `docs/TODO.md`

**Interfaces:**
- Consumes: `USAGE_LEDGER_URL` in the router and `DATABASE_URL` in the ledger service.
- Produces: internal router-to-ledger-to-Postgres flow.

- [ ] **Step 1: Wire Docker services**

In `src/docker-compose.yml`, add to `sticky-router.environment`:

```yaml
      USAGE_LEDGER_URL: http://usage-ledger:4200
```

Add service:

```yaml
  usage-ledger:
    build:
      context: ./ledger
    container_name: ai-gateway-usage-ledger
    restart: unless-stopped
    env_file:
      - .env
    environment:
      DATABASE_URL: ${DATABASE_URL}
    expose:
      - "4200"
    depends_on:
      postgres:
        condition: service_healthy
    healthcheck:
      test:
        - CMD-SHELL
        - python -c "import urllib.request; urllib.request.urlopen('http://localhost:4200/healthz')"
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 20s
```

No host port is published for `usage-ledger`.

- [ ] **Step 2: Document the ledger layer**

Add to `README.md` and `src/README.md`:

```markdown
## Persistent Usage Ledger

The router emits prompt-free usage events to an internal `usage-ledger` service.
Only the ledger service writes the gateway-owned `gateway_usage_events` table in
the existing Postgres database. The router does not import a Postgres driver or
write SQL directly.

The ledger stores hashed key/session identifiers, model aliases, provider model,
status, latency, token counts when upstream returns them, estimated cost when
pricing is configured, cache status, and fallback metadata. It does not store
prompt bodies, response bodies, raw bearer tokens, or raw session IDs.

Inspect recent rows:

```bash
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
select to_timestamp(timestamp), served_model, status, latency_ms,
       total_tokens, estimated_cost_usd, cache_status, fallback_count
from gateway_usage_events
order by id desc
limit 20;
"
```
```

- [ ] **Step 3: Update TODO status**

In `docs/TODO.md`, add under `P2: Cost And Observability`:

```markdown
- [x] Add a prompt-free persistent usage ledger through an internal ledger service.
```

Add under `P3: Future`:

```markdown
- [ ] Add a small daily/monthly ledger summary command once enough rows exist.
```

- [ ] **Step 4: Verify Docker and docs**

Run:

```bash
docker compose -f src/docker-compose.yml config >/tmp/ai-gateway-compose-rendered.yaml
rg -n "usage-ledger|gateway_usage_events|Persistent Usage Ledger" README.md src/README.md src/docker-compose.yml docs/TODO.md
```

Expected: compose config exits with status 0, and docs mention the ledger layer.

- [ ] **Step 5: Commit Task 5**

```bash
git add src/docker-compose.yml README.md src/README.md docs/TODO.md
git commit -m "docs: wire usage ledger service"
```

---

### Task 6: SDK And Path Compatibility Tests

**Files:**
- Create: `src/router/tests/test_compatibility_paths.py`
- Modify: `README.md`
- Modify: `src/README.md`
- Modify: `docs/TODO.md`

**Interfaces:**
- Consumes: existing router endpoints.
- Produces: compatibility tests for supported paths and explicit tests for unsupported `/v1/*` paths.

- [ ] **Step 1: Write compatibility path tests**

Create `src/router/tests/test_compatibility_paths.py`:

```python
import json

import httpx
import pytest

from router.main import create_app


@pytest.mark.asyncio
async def test_openai_chat_compat_preserves_auth_content_type_and_gateway_headers():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}}]})

    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=httpx.MockTransport(handler))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer virtual-key", "Content-Type": "application/json"},
            json={"model": "coder", "messages": [{"role": "user", "content": "say OK"}]},
        )

    assert response.status_code == 200
    assert seen["url"] == "http://litellm:4000/v1/chat/completions"
    assert seen["authorization"] == "Bearer virtual-key"
    assert seen["content_type"] == "application/json"
    assert seen["body"]["model"] == "coder"
    assert response.headers["X-Gateway-Model"] == "coder"


@pytest.mark.asyncio
async def test_models_compat_preserves_auth_and_response_body():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"object": "list", "data": [{"id": "coder", "object": "model"}]})

    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=httpx.MockTransport(handler))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/models", headers={"Authorization": "Bearer virtual-key"})

    assert response.status_code == 200
    assert seen == {"url": "http://litellm:4000/v1/models", "authorization": "Bearer virtual-key"}
    assert response.json() == {"object": "list", "data": [{"id": "coder", "object": "model"}]}


@pytest.mark.asyncio
async def test_streaming_chat_compat_preserves_sse_content_type_and_body():
    sse_body = b'data: {"choices":[{"delta":{"content":"O"}}]}\n\ndata: [DONE]\n\n'

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse_body, headers={"content-type": "text/event-stream"})

    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=httpx.MockTransport(handler))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer virtual-key"},
            json={"model": "coder", "messages": [{"role": "user", "content": "say OK"}], "stream": True},
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    assert response.content == sse_body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("POST", "/v1/responses", {"model": "coder", "input": "say OK"}),
        ("POST", "/v1/embeddings", {"model": "text-embedding-3-small", "input": "hello"}),
        ("POST", "/v1/images/generations", {"model": "gpt-image-1", "prompt": "a square"}),
        ("GET", "/v1/files", None),
    ],
)
async def test_unsupported_openai_paths_return_clear_501(method, path, json_body):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unsupported paths must not be proxied")

    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, transport=httpx.MockTransport(handler))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, path, json=json_body)

    assert response.status_code == 501
    assert response.json() == {"error": f"{path} is not implemented by the sticky router"}
```

- [ ] **Step 2: Document compatibility matrix**

Add to `README.md` and `src/README.md`:

```markdown
## Compatibility Contract

The router is tested for OpenAI-compatible chat, streaming chat, and model
discovery behavior. It forwards `Authorization`, JSON content type, and the
request body to LiteLLM for supported paths. Unsupported `/v1/*` paths return
HTTP `501` and are not proxied.

| Client behavior | Path | Status |
| --- | --- | --- |
| Chat completions | `/v1/chat/completions` | Supported |
| Streaming chat completions | `/v1/chat/completions` with `stream: true` | Supported |
| Model discovery | `/v1/models` | Supported |
| Responses API | `/v1/responses` | Explicit `501` |
| Embeddings | `/v1/embeddings` | Explicit `501` |
| Images | `/v1/images/*` | Explicit `501` |
| Files | `/v1/files` | Explicit `501` |
```

- [ ] **Step 3: Verify Task 6**

Run:

```bash
PYTHONPATH=src python3 -m pytest src/router/tests/test_compatibility_paths.py src/router/tests/test_app.py src/router/tests/test_streaming.py -q
rg -n "Compatibility Contract|/v1/responses|/v1/embeddings" README.md src/README.md docs/TODO.md
```

Expected: tests PASS, and docs contain the matrix.

- [ ] **Step 4: Commit Task 6**

```bash
git add src/router/tests/test_compatibility_paths.py README.md src/README.md docs/TODO.md
git commit -m "test: lock openai compatibility paths"
```

---

### Task 7: Full Verification And Simplification Review

**Files:**
- Inspect all files changed by Tasks 1-6.

**Interfaces:**
- Consumes: complete feature diff.
- Produces: verified, simplified implementation ready for branch completion.

- [ ] **Step 1: Run full test and quality suite**

Run:

```bash
PYTHONPATH=src python3 -m pytest src/router/tests src/ledger/tests -q
python3 -m ruff check src/router src/ledger src/scripts
python3 -m mypy src/router
docker compose -f src/docker-compose.yml config >/tmp/ai-gateway-compose-rendered.yaml
```

Expected: all PASS.

- [ ] **Step 2: Verify generated config is current**

Run:

```bash
python3 src/scripts/generate_configs.py
git diff -- src/router/router_config.yaml src/litellm.config.yaml
```

Expected: no diff after regeneration.

- [ ] **Step 3: Run ponytail review**

Invoke the `ponytail-review` skill on the resulting diff. Apply simplifications that remove unnecessary code without weakening these requirements:

- prompt-free ledger
- best-effort router-to-ledger emission
- existing Postgres storage
- router has no DB driver dependency
- compatibility tests for supported and unsupported paths
- no new database service

- [ ] **Step 4: Run final verification after simplifications**

Run:

```bash
PYTHONPATH=src python3 -m pytest src/router/tests src/ledger/tests -q
python3 -m ruff check src/router src/ledger src/scripts
python3 -m mypy src/router
```

Expected: all PASS.

- [ ] **Step 5: Commit final simplifications if any**

If Task 7 changed files:

```bash
git add <changed-files>
git commit -m "refactor: simplify usage ledger implementation"
```

If Task 7 made no changes, do not create an empty commit.


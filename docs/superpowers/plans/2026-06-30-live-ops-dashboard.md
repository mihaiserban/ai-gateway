# Live Ops Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a router-hosted dashboard for live gateway operations and 30-day usage statistics.

**Architecture:** Serve a small vanilla HTML/CSS/JS dashboard from the existing FastAPI router. Live state comes from router health, readiness, route config, and in-memory `/metrics`; historical stats come from the usage ledger plan's router-owned Postgres table, `gateway_usage_events`.

**Tech Stack:** FastAPI, httpx, pytest, pytest-asyncio, Postgres via `psycopg[binary]`, existing router metrics, existing usage ledger, vanilla browser JavaScript, no new frontend build step.

## Global Constraints

- Prerequisite plan: `docs/superpowers/plans/2026-06-30-usage-ledger-compatibility-tests.md` must be implemented first.
- Do not add a second persistence engine. Historical stats read from `gateway_usage_events` in the existing Postgres database.
- Do not depend on LiteLLM private database tables for dashboard statistics.
- Keep `/metrics` as the in-memory debug endpoint; dashboard APIs may reuse the same `Metrics.snapshot()` data.
- Store and display only metadata. Do not store or render prompt bodies, response bodies, raw bearer tokens, or raw session IDs.
- Default historical window is 30 days. Allow `1`, `7`, and `30` day windows in the UI/API.
- Keep the dashboard read-only.
- Keep the router as the only published service on port `4100`; do not expose LiteLLM port `4000`.
- Do not add a frontend package manager, React, charting dependency, or template engine. Use vanilla browser APIs and inline SVG/CSS bars.
- Use TDD for each task: write the failing test first, run it, implement the smallest change, run focused tests, then commit.
- Respect the existing dirty worktree. Do not revert unrelated changes.
- After implementation, invoke the `ponytail-review` skill on the resulting diff before declaring completion.

---

## Dependency Gate

Before starting Task 1, verify the usage-ledger plan is complete in the working tree:

```bash
test -f src/router/usage_ledger.py
rg -n "gateway_usage_events|model_prices|usage_ledger" src/router src/docker-compose.yml README.md src/README.md
PYTHONPATH=src python3 -m pytest \
  src/router/tests/test_usage_ledger.py \
  src/router/tests/test_usage_ledger_integration.py \
  src/router/tests/test_compatibility_paths.py \
  -q
```

Expected: files and docs mention `gateway_usage_events`, and the ledger/compatibility tests pass.

---

## File Structure

- Create `src/router/dashboard.py`: dashboard HTML response, live JSON payload builder, Postgres usage summary queries, safe period parsing, and FastAPI route registration helper.
- Create `src/router/tests/test_dashboard.py`: tests for period parsing, live payload shape, usage SQL summaries, and route behavior.
- Modify `src/router/main.py`: register dashboard routes in `create_app()`.
- Modify `README.md`: document dashboard URL, data sources, 30-day default, and privacy posture.
- Modify `src/README.md`: add NAS runbook notes for dashboard use.
- Modify `docs/TODO.md`: mark live operations dashboard as active/completed according to the final result.

---

### Task 1: Dashboard API Unit With Safe Period Parsing

**Files:**
- Create: `src/router/dashboard.py`
- Create: `src/router/tests/test_dashboard.py`

**Interfaces:**
- Consumes: `Metrics.snapshot()` shape from `src/router/metrics.py`.
- Consumes: route config fields `default_model`, `allowed_models`, `fallbacks`, and `provider_models`.
- Produces: `parse_days(value: str | None) -> int`.
- Produces: `live_payload(app_state: Any, health: dict[str, str], readiness: dict[str, str]) -> dict[str, Any]`.

- [ ] **Step 1: Write failing tests for period parsing and live payload**

Create `src/router/tests/test_dashboard.py`:

```python
from types import SimpleNamespace

import pytest

from router.dashboard import live_payload, parse_days
from router.metrics import Metrics
from router.routing import RouteConfig


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 30),
        ("", 30),
        ("1", 1),
        ("7", 7),
        ("30", 30),
        ("999", 30),
        ("nope", 30),
    ],
)
def test_parse_days_allows_only_supported_windows(value, expected):
    assert parse_days(value) == expected


def test_live_payload_combines_health_metrics_and_config():
    metrics = Metrics()
    metrics.record("coder", "coder", 0, cache_hit=True)
    state = SimpleNamespace(
        metrics=metrics,
        route_config=RouteConfig(
            default_model="coder",
            allowed_models={"coder", "planner"},
            fallbacks={"coder": ["planner"], "planner": []},
            provider_models={"coder": "ollama_chat/kimi-k2.7-code"},
        ),
    )

    payload = live_payload(
        state,
        health={"router": "ok", "litellm": "ok", "redis": "ok", "postgres": "ok", "status": "ok"},
        readiness={"router": "ok", "litellm": "ok", "redis": "ok", "postgres": "ok", "status": "ready"},
    )

    assert payload["health"]["status"] == "ok"
    assert payload["readiness"]["status"] == "ready"
    assert payload["metrics"]["requests_total"] == 1
    assert payload["metrics"]["cache_counts"] == {"hit": 1, "miss": 0, "unknown": 0}
    assert payload["config"]["default_model"] == "coder"
    assert payload["config"]["allowed_models"] == ["coder", "planner"]
    assert payload["config"]["fallbacks"] == {"coder": ["planner"], "planner": []}
    assert payload["config"]["provider_models"] == {"coder": "ollama_chat/kimi-k2.7-code"}
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
PYTHONPATH=src python3 -m pytest src/router/tests/test_dashboard.py::test_parse_days_allows_only_supported_windows src/router/tests/test_dashboard.py::test_live_payload_combines_health_metrics_and_config -q
```

Expected: FAIL because `router.dashboard` does not exist.

- [ ] **Step 3: Implement the minimal dashboard helpers**

Create `src/router/dashboard.py`:

```python
from __future__ import annotations

from typing import Any


SUPPORTED_WINDOWS = {1, 7, 30}
DEFAULT_WINDOW_DAYS = 30


def parse_days(value: str | None) -> int:
    try:
        days = int(value or DEFAULT_WINDOW_DAYS)
    except ValueError:
        return DEFAULT_WINDOW_DAYS
    return days if days in SUPPORTED_WINDOWS else DEFAULT_WINDOW_DAYS


def live_payload(app_state: Any, health: dict[str, str], readiness: dict[str, str]) -> dict[str, Any]:
    config = app_state.route_config
    return {
        "health": health,
        "readiness": readiness,
        "metrics": app_state.metrics.snapshot(),
        "config": {
            "default_model": config.default_model,
            "allowed_models": sorted(config.allowed_models),
            "fallbacks": {key: list(value) for key, value in sorted(config.fallbacks.items())},
            "provider_models": dict(sorted(config.provider_models.items())),
        },
    }
```

- [ ] **Step 4: Run the focused tests**

Run:

```bash
PYTHONPATH=src python3 -m pytest src/router/tests/test_dashboard.py::test_parse_days_allows_only_supported_windows src/router/tests/test_dashboard.py::test_live_payload_combines_health_metrics_and_config -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/router/dashboard.py src/router/tests/test_dashboard.py
git commit -m "feat: add dashboard live payload helpers"
```

---

### Task 2: Usage Summary Reader For The Ledger

**Files:**
- Modify: `src/router/dashboard.py`
- Modify: `src/router/tests/test_dashboard.py`

**Interfaces:**
- Consumes: `gateway_usage_events` table created by the usage-ledger plan.
- Produces: `UsageSummaryStore(database_url: str | None, connect: Callable[[str], Any] = psycopg.connect)`.
- Produces: `UsageSummaryStore.summary(days: int) -> dict[str, Any]`.

- [ ] **Step 1: Add fake-connection tests for usage summary SQL**

Append to `src/router/tests/test_dashboard.py`:

```python
from router.dashboard import UsageSummaryStore


class FakeCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.results: list[list[dict[str, object]]] = [
            [
                {
                    "requests": 4,
                    "prompt_tokens": 30,
                    "completion_tokens": 20,
                    "total_tokens": 50,
                    "estimated_cost_usd": 1.25,
                    "avg_latency_ms": 125.0,
                    "fallback_count": 2,
                    "cache_hits": 1,
                    "cache_misses": 2,
                    "cache_unknown": 1,
                }
            ],
            [
                {
                    "served_model": "coder",
                    "requests": 3,
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "total_tokens": 30,
                    "estimated_cost_usd": 1.0,
                    "avg_latency_ms": 100.0,
                }
            ],
            [
                {
                    "day": "2026-06-30",
                    "requests": 4,
                    "total_tokens": 50,
                    "estimated_cost_usd": 1.25,
                }
            ],
            [
                {
                    "key_hash": "abc123",
                    "requests": 4,
                    "total_tokens": 50,
                    "estimated_cost_usd": 1.25,
                }
            ],
            [
                {
                    "timestamp": 1782820800.0,
                    "served_model": "coder",
                    "provider_model": "ollama_chat/kimi-k2.7-code",
                    "status": "503",
                    "error_class": "http_503",
                    "latency_ms": 250,
                    "fallback_count": 1,
                }
            ],
        ]

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.calls.append((sql, params))

    def fetchall(self) -> list[dict[str, object]]:
        return self.results.pop(0)


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_obj = FakeCursor()

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def cursor(self, *args, **kwargs) -> FakeCursor:
        return self.cursor_obj


def test_usage_summary_store_returns_empty_payload_without_database_url():
    assert UsageSummaryStore(None).summary(30) == {
        "enabled": False,
        "period_days": 30,
        "totals": {},
        "top_models": [],
        "daily_usage": [],
        "top_keys": [],
        "recent_failures": [],
    }


def test_usage_summary_store_reads_ledger_with_window_filter():
    fake = FakeConnection()
    store = UsageSummaryStore("postgresql://example", connect=lambda _: fake)

    summary = store.summary(30)

    assert summary["enabled"] is True
    assert summary["period_days"] == 30
    assert summary["totals"]["requests"] == 4
    assert summary["top_models"][0]["served_model"] == "coder"
    assert summary["daily_usage"][0]["day"] == "2026-06-30"
    assert summary["top_keys"][0]["key_hash"] == "abc123"
    assert summary["recent_failures"][0]["status"] == "503"
    assert len(fake.cursor_obj.calls) == 5
    for _, params in fake.cursor_obj.calls:
        assert params == (30,)
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
PYTHONPATH=src python3 -m pytest src/router/tests/test_dashboard.py::test_usage_summary_store_returns_empty_payload_without_database_url src/router/tests/test_dashboard.py::test_usage_summary_store_reads_ledger_with_window_filter -q
```

Expected: FAIL because `UsageSummaryStore` does not exist.

- [ ] **Step 3: Implement usage summary queries**

Extend `src/router/dashboard.py`:

```python
from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row
```

Add:

```python
class UsageSummaryStore:
    def __init__(
        self,
        database_url: str | None,
        *,
        connect: Callable[[str], Any] = psycopg.connect,
    ) -> None:
        self.database_url = database_url
        self._connect = connect

    def summary(self, days: int) -> dict[str, Any]:
        if not self.database_url:
            return _empty_usage_summary(days)

        with self._connect(self.database_url) as conn:
            cur = conn.cursor(row_factory=dict_row)
            totals = _fetch_one(cur, TOTALS_SQL, days)
            top_models = _fetch_all(cur, TOP_MODELS_SQL, days)
            daily_usage = _fetch_all(cur, DAILY_USAGE_SQL, days)
            top_keys = _fetch_all(cur, TOP_KEYS_SQL, days)
            recent_failures = _fetch_all(cur, RECENT_FAILURES_SQL, days)

        return {
            "enabled": True,
            "period_days": days,
            "totals": totals,
            "top_models": top_models,
            "daily_usage": daily_usage,
            "top_keys": top_keys,
            "recent_failures": recent_failures,
        }


def _empty_usage_summary(days: int) -> dict[str, Any]:
    return {
        "enabled": False,
        "period_days": days,
        "totals": {},
        "top_models": [],
        "daily_usage": [],
        "top_keys": [],
        "recent_failures": [],
    }


def _fetch_one(cur: Any, sql: str, days: int) -> dict[str, Any]:
    cur.execute(sql, (days,))
    rows = cur.fetchall()
    return dict(rows[0]) if rows else {}


def _fetch_all(cur: Any, sql: str, days: int) -> list[dict[str, Any]]:
    cur.execute(sql, (days,))
    return [dict(row) for row in cur.fetchall()]


WINDOW_FILTER = "timestamp >= extract(epoch from now() - (%s * interval '1 day'))"

TOTALS_SQL = f"""
select
    count(*)::int as requests,
    coalesce(sum(prompt_tokens), 0)::int as prompt_tokens,
    coalesce(sum(completion_tokens), 0)::int as completion_tokens,
    coalesce(sum(total_tokens), 0)::int as total_tokens,
    coalesce(sum(estimated_cost_usd), 0)::float as estimated_cost_usd,
    coalesce(avg(latency_ms), 0)::float as avg_latency_ms,
    coalesce(sum(fallback_count), 0)::int as fallback_count,
    count(*) filter (where cache_status = 'hit')::int as cache_hits,
    count(*) filter (where cache_status = 'miss')::int as cache_misses,
    count(*) filter (where cache_status = 'unknown')::int as cache_unknown
from gateway_usage_events
where {WINDOW_FILTER}
"""

TOP_MODELS_SQL = f"""
select
    served_model,
    count(*)::int as requests,
    coalesce(sum(prompt_tokens), 0)::int as prompt_tokens,
    coalesce(sum(completion_tokens), 0)::int as completion_tokens,
    coalesce(sum(total_tokens), 0)::int as total_tokens,
    coalesce(sum(estimated_cost_usd), 0)::float as estimated_cost_usd,
    coalesce(avg(latency_ms), 0)::float as avg_latency_ms
from gateway_usage_events
where {WINDOW_FILTER}
group by served_model
order by total_tokens desc, requests desc, served_model asc
limit 10
"""

DAILY_USAGE_SQL = f"""
select
    to_char(to_timestamp(timestamp)::date, 'YYYY-MM-DD') as day,
    count(*)::int as requests,
    coalesce(sum(total_tokens), 0)::int as total_tokens,
    coalesce(sum(estimated_cost_usd), 0)::float as estimated_cost_usd
from gateway_usage_events
where {WINDOW_FILTER}
group by day
order by day asc
"""

TOP_KEYS_SQL = f"""
select
    key_hash,
    count(*)::int as requests,
    coalesce(sum(total_tokens), 0)::int as total_tokens,
    coalesce(sum(estimated_cost_usd), 0)::float as estimated_cost_usd
from gateway_usage_events
where {WINDOW_FILTER}
group by key_hash
order by total_tokens desc, requests desc, key_hash asc
limit 10
"""

RECENT_FAILURES_SQL = f"""
select
    timestamp,
    served_model,
    provider_model,
    status,
    error_class,
    latency_ms,
    fallback_count
from gateway_usage_events
where {WINDOW_FILTER}
  and (status !~ '^2' or error_class is not null)
order by timestamp desc
limit 20
"""
```

- [ ] **Step 4: Run the focused tests**

Run:

```bash
PYTHONPATH=src python3 -m pytest src/router/tests/test_dashboard.py::test_usage_summary_store_returns_empty_payload_without_database_url src/router/tests/test_dashboard.py::test_usage_summary_store_reads_ledger_with_window_filter -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/router/dashboard.py src/router/tests/test_dashboard.py
git commit -m "feat: read dashboard usage summaries"
```

---

### Task 3: FastAPI Dashboard Routes

**Files:**
- Modify: `src/router/dashboard.py`
- Modify: `src/router/main.py`
- Modify: `src/router/tests/test_dashboard.py`

**Interfaces:**
- Consumes: `gather_health()` and `all_ready()` from `src/router/health.py`.
- Produces:
  - `GET /dashboard` HTML page.
  - `GET /dashboard/api/live` JSON.
  - `GET /dashboard/api/usage?days=30` JSON.
  - `register_dashboard(app: FastAPI) -> None`.

- [ ] **Step 1: Add failing route tests**

Append to `src/router/tests/test_dashboard.py`:

```python
import httpx
import pytest

from router.main import create_app


@pytest.mark.asyncio
async def test_dashboard_html_route_returns_page():
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, database_url=None)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/dashboard")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "AI Gateway" in response.text
    assert "/dashboard/api/live" in response.text
    assert "/dashboard/api/usage" in response.text


@pytest.mark.asyncio
async def test_dashboard_live_api_returns_json_shape():
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, database_url=None)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/dashboard/api/live")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"health", "readiness", "metrics", "config"}
    assert payload["config"]["default_model"] == "coder"


@pytest.mark.asyncio
async def test_dashboard_usage_api_defaults_to_30_days_when_ledger_disabled():
    app = create_app(litellm_base_url="http://litellm:4000", redis_url=None, database_url=None)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/dashboard/api/usage?days=999")

    assert response.status_code == 200
    assert response.json() == {
        "enabled": False,
        "period_days": 30,
        "totals": {},
        "top_models": [],
        "daily_usage": [],
        "top_keys": [],
        "recent_failures": [],
    }
```

- [ ] **Step 2: Run route tests and verify they fail**

Run:

```bash
PYTHONPATH=src python3 -m pytest src/router/tests/test_dashboard.py::test_dashboard_html_route_returns_page src/router/tests/test_dashboard.py::test_dashboard_live_api_returns_json_shape src/router/tests/test_dashboard.py::test_dashboard_usage_api_defaults_to_30_days_when_ledger_disabled -q
```

Expected: FAIL because routes are not registered.

- [ ] **Step 3: Implement route registration and HTML shell**

Extend `src/router/dashboard.py`:

```python
import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from router.health import all_ready, gather_health
```

Add:

```python
def register_dashboard(app: FastAPI) -> None:
    app.state.usage_summary_store = UsageSummaryStore(getattr(app.state, "database_url", None))

    @app.get("/dashboard", include_in_schema=False)
    async def dashboard_page() -> HTMLResponse:
        return HTMLResponse(DASHBOARD_HTML)

    @app.get("/dashboard/api/live", include_in_schema=False)
    async def dashboard_live() -> JSONResponse:
        health = await gather_health(app.state)
        readiness = dict(health)
        readiness["status"] = "ready" if all_ready(readiness) else "not ready"
        return JSONResponse(live_payload(app.state, health, readiness))

    @app.get("/dashboard/api/usage", include_in_schema=False)
    async def dashboard_usage(request: Request) -> JSONResponse:
        days = parse_days(request.query_params.get("days"))
        summary = await asyncio.to_thread(app.state.usage_summary_store.summary, days)
        return JSONResponse(summary)
```

Add a compact `DASHBOARD_HTML` constant in the same file:

```python
DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI Gateway Dashboard</title>
  <style>
    :root { color-scheme: light dark; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    body { margin: 0; background: #f6f7f9; color: #18202a; }
    main { max-width: 1180px; margin: 0 auto; padding: 24px; }
    header { display: flex; justify-content: space-between; gap: 16px; align-items: center; margin-bottom: 20px; }
    h1 { margin: 0; font-size: 28px; }
    h2 { font-size: 16px; margin: 0 0 12px; }
    button { border: 1px solid #b8c0cc; background: white; border-radius: 6px; padding: 8px 10px; cursor: pointer; }
    button[aria-pressed="true"] { background: #17202a; color: white; border-color: #17202a; }
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 12px; }
    .panel { background: white; border: 1px solid #dfe3e8; border-radius: 8px; padding: 14px; }
    .span-3 { grid-column: span 3; }
    .span-4 { grid-column: span 4; }
    .span-6 { grid-column: span 6; }
    .span-8 { grid-column: span 8; }
    .span-12 { grid-column: span 12; }
    .metric { font-size: 30px; font-weight: 700; }
    .muted { color: #5d6875; font-size: 13px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { border-bottom: 1px solid #e8ebef; padding: 8px; text-align: left; }
    .bar { height: 8px; border-radius: 999px; background: #dbe7ff; overflow: hidden; }
    .bar > span { display: block; height: 100%; background: #2563eb; }
    @media (max-width: 800px) { .span-3, .span-4, .span-6, .span-8 { grid-column: span 12; } header { align-items: flex-start; flex-direction: column; } }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>AI Gateway</h1>
        <div id="updated" class="muted">Loading...</div>
      </div>
      <div>
        <button data-days="1">24h</button>
        <button data-days="7">7d</button>
        <button data-days="30" aria-pressed="true">30d</button>
      </div>
    </header>
    <section class="grid">
      <div class="panel span-3"><h2>Readiness</h2><div id="readiness" class="metric">-</div></div>
      <div class="panel span-3"><h2>Requests</h2><div id="requests" class="metric">-</div></div>
      <div class="panel span-3"><h2>Tokens</h2><div id="tokens" class="metric">-</div></div>
      <div class="panel span-3"><h2>Est. Spend</h2><div id="spend" class="metric">-</div></div>
      <div class="panel span-6"><h2>Top Models</h2><div id="models"></div></div>
      <div class="panel span-6"><h2>Daily Usage</h2><div id="daily"></div></div>
      <div class="panel span-6"><h2>Provider Availability</h2><div id="availability"></div></div>
      <div class="panel span-6"><h2>Recent Failures</h2><div id="failures"></div></div>
    </section>
  </main>
  <script>
    let selectedDays = 30;
    const fmt = new Intl.NumberFormat();
    const usd = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 4 });

    document.querySelectorAll("button[data-days]").forEach((button) => {
      button.addEventListener("click", () => {
        selectedDays = Number(button.dataset.days);
        document.querySelectorAll("button[data-days]").forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
        refresh();
      });
    });

    async function refresh() {
      const [live, usage] = await Promise.all([
        fetch("/dashboard/api/live").then((response) => response.json()),
        fetch(`/dashboard/api/usage?days=${selectedDays}`).then((response) => response.json()),
      ]);
      render(live, usage);
    }

    function render(live, usage) {
      document.getElementById("updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
      document.getElementById("readiness").textContent = live.readiness.status;
      document.getElementById("requests").textContent = fmt.format(usage.totals.requests || live.metrics.requests_total || 0);
      document.getElementById("tokens").textContent = fmt.format(usage.totals.total_tokens || 0);
      document.getElementById("spend").textContent = usd.format(usage.totals.estimated_cost_usd || 0);
      renderModels(usage.top_models || []);
      renderDaily(usage.daily_usage || []);
      renderAvailability(live.metrics.provider_availability || {});
      renderFailures(usage.recent_failures || []);
    }

    function renderModels(rows) {
      const max = Math.max(1, ...rows.map((row) => row.total_tokens || 0));
      document.getElementById("models").innerHTML = table(["Model", "Requests", "Tokens", "Spend"], rows.map((row) => [
        row.served_model,
        fmt.format(row.requests || 0),
        `${fmt.format(row.total_tokens || 0)}<div class="bar"><span style="width:${Math.round(((row.total_tokens || 0) / max) * 100)}%"></span></div>`,
        usd.format(row.estimated_cost_usd || 0),
      ]));
    }

    function renderDaily(rows) {
      const max = Math.max(1, ...rows.map((row) => row.total_tokens || 0));
      document.getElementById("daily").innerHTML = table(["Day", "Requests", "Tokens"], rows.map((row) => [
        row.day,
        fmt.format(row.requests || 0),
        `${fmt.format(row.total_tokens || 0)}<div class="bar"><span style="width:${Math.round(((row.total_tokens || 0) / max) * 100)}%"></span></div>`,
      ]));
    }

    function renderAvailability(map) {
      const rows = Object.entries(map).map(([model, value]) => [
        model,
        fmt.format(value.attempts || 0),
        `${value.availability_percent || 0}%`,
        value.last_status || "",
      ]);
      document.getElementById("availability").innerHTML = table(["Model", "Attempts", "Availability", "Last"], rows);
    }

    function renderFailures(rows) {
      document.getElementById("failures").innerHTML = table(["Model", "Status", "Error", "Fallbacks"], rows.map((row) => [
        row.served_model,
        row.status,
        row.error_class || "",
        fmt.format(row.fallback_count || 0),
      ]));
    }

    function table(headers, rows) {
      if (!rows.length) return '<div class="muted">No data yet.</div>';
      return `<table><thead><tr>${headers.map((header) => `<th>${header}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${row.map((cell) => `<td>${cell}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
    }

    refresh();
    setInterval(refresh, 30000);
  </script>
</body>
</html>"""
```

Modify `src/router/main.py`:

```python
from router.dashboard import register_dashboard
```

Call this after `app.state.async_sleep = asyncio.sleep`:

```python
    register_dashboard(app)
```

- [ ] **Step 4: Run dashboard route tests**

Run:

```bash
PYTHONPATH=src python3 -m pytest src/router/tests/test_dashboard.py::test_dashboard_html_route_returns_page src/router/tests/test_dashboard.py::test_dashboard_live_api_returns_json_shape src/router/tests/test_dashboard.py::test_dashboard_usage_api_defaults_to_30_days_when_ledger_disabled -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add src/router/dashboard.py src/router/main.py src/router/tests/test_dashboard.py
git commit -m "feat: serve live ops dashboard"
```

---

### Task 4: Dashboard Integration And Regression Coverage

**Files:**
- Modify: `src/router/tests/test_dashboard.py`

**Interfaces:**
- Consumes: all dashboard endpoints from Task 3.
- Produces: regression coverage proving dashboard routes do not break existing OpenAI-compatible routes.

- [ ] **Step 1: Add route coexistence tests**

Append to `src/router/tests/test_dashboard.py`:

```python
@pytest.mark.asyncio
async def test_dashboard_routes_do_not_break_chat_or_metrics():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            },
        )

    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        database_url=None,
        transport=httpx.MockTransport(handler),
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        chat = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test", "X-Session-Id": "dash-regression"},
            json={"model": "coder", "messages": [{"role": "user", "content": "say OK"}]},
        )
        metrics = await client.get("/metrics")
        live = await client.get("/dashboard/api/live")

    assert chat.status_code == 200
    assert metrics.status_code == 200
    assert live.status_code == 200
    assert metrics.json()["requests_total"] == 1
    assert live.json()["metrics"]["requests_total"] == 1
```

- [ ] **Step 2: Run focused regression tests**

Run:

```bash
PYTHONPATH=src python3 -m pytest src/router/tests/test_dashboard.py src/router/tests/test_metrics.py src/router/tests/test_app.py -q
```

Expected: PASS.

- [ ] **Step 3: Run quality checks**

Run:

```bash
python3 -m ruff check src/router/dashboard.py src/router/main.py src/router/tests/test_dashboard.py
python3 -m mypy src/router
```

Expected: PASS.

- [ ] **Step 4: Commit Task 4**

```bash
git add src/router/tests/test_dashboard.py
git commit -m "test: cover dashboard route coexistence"
```

---

### Task 5: Documentation And Operations Notes

**Files:**
- Modify: `README.md`
- Modify: `src/README.md`
- Modify: `docs/TODO.md`

**Interfaces:**
- Consumes: dashboard routes from Task 3.
- Produces: runbook docs for using and verifying the dashboard.

- [ ] **Step 1: Document the dashboard in the root README**

Add this section after the `/metrics` documentation in `README.md`:

```markdown
## Live Operations Dashboard

The router serves a read-only dashboard at:

```text
http://<host>:4100/dashboard
```

The dashboard combines live router state with the prompt-free Postgres usage
ledger:

- `/dashboard/api/live` reads health, readiness, router metrics, and routing
  config.
- `/dashboard/api/usage?days=30` reads `gateway_usage_events` for top models,
  daily usage, token counts, estimated spend, top hashed key IDs, and recent
  failures.

The default statistics window is 30 days. The UI also supports 24-hour and
7-day views. It does not display prompts, responses, raw bearer tokens, or raw
session IDs.
```

- [ ] **Step 2: Add NAS runbook notes**

Add this section near the router metrics section in `src/README.md`:

```markdown
## Dashboard

Open the live operations dashboard from the LAN or Tailscale network:

```text
http://<nas-host>:4100/dashboard
```

Use it to check dependency readiness, request volume, fallback behavior, cache
counts, provider availability, token usage by model, estimated spend, and recent
failed upstream attempts. Historical cards use the router-owned
`gateway_usage_events` table and default to the last 30 days.
```

- [ ] **Step 3: Update TODO**

In `docs/TODO.md`, add under `P2: Cost And Observability`:

```markdown
- [x] Add a read-only live operations dashboard backed by `/metrics` and the
      Postgres usage ledger.
```

- [ ] **Step 4: Verify docs**

Run:

```bash
rg -n "Live Operations Dashboard|/dashboard|gateway_usage_events|30 days" README.md src/README.md docs/TODO.md
```

Expected: all three files mention the dashboard and the 30-day ledger-backed stats.

- [ ] **Step 5: Commit Task 5**

```bash
git add README.md src/README.md docs/TODO.md
git commit -m "docs: document live ops dashboard"
```

---

### Task 6: Full Verification And Completion Review

**Files:**
- No new files unless `ponytail-review` finds a required simplification.

**Interfaces:**
- Consumes: full implementation diff.
- Produces: verified dashboard implementation ready for review.

- [ ] **Step 1: Run the full router test suite**

Run:

```bash
PYTHONPATH=src python3 -m pytest src/router/tests -q
```

Expected: PASS.

- [ ] **Step 2: Validate generated Docker config**

Run:

```bash
docker compose -f src/docker-compose.yml config >/tmp/ai-gateway-compose-rendered.yaml
```

Expected: exits with status 0.

- [ ] **Step 3: Run static checks**

Run:

```bash
python3 -m ruff check src/router
python3 -m mypy src/router
```

Expected: PASS.

- [ ] **Step 4: Invoke ponytail-review on the resulting diff**

Use the `ponytail-review` skill on the implementation diff. Apply simplification suggestions unless they conflict with the dashboard requirements, tests, or privacy constraints.

- [ ] **Step 5: Run final verification after simplifications**

Run:

```bash
PYTHONPATH=src python3 -m pytest src/router/tests -q
python3 -m ruff check src/router
python3 -m mypy src/router
```

Expected: PASS.

- [ ] **Step 6: Commit final simplifications if any**

If `ponytail-review` caused changes:

```bash
git add src/router README.md src/README.md docs/TODO.md
git commit -m "refactor: simplify dashboard implementation"
```

---

## Self-Review

- Spec coverage: The plan covers the explicit dependency on the usage-ledger plan, live health/metrics/config data, 30-day default historical stats, token counts per model, top models, period filters, UI, docs, tests, and completion review.
- Placeholder scan: No `TBD`, `TODO`, or vague "add tests" instructions remain. Each implementation step includes exact files, code, commands, and expected outcomes.
- Type consistency: `parse_days`, `live_payload`, `UsageSummaryStore.summary`, and `register_dashboard` names are consistent across tasks. The dashboard reads the `gateway_usage_events` columns defined in the ledger plan.

## Execution Options

Plan complete and saved to `docs/superpowers/plans/2026-06-30-live-ops-dashboard.md`. Two execution options:

**1. Subagent-Driven (recommended)** - Dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?

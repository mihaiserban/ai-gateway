from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from psycopg.rows import dict_row

from router.health import all_ready, gather_health

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

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
    return {"enabled": False, "period_days": days, "totals": {}} | {
        key: [] for key in ("top_models", "daily_usage", "top_keys", "recent_failures")
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
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Rubik:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --canvas: #1f1633;
      --night: #150f23;
      --panel: #150f23;
      --ink: #ffffff;
      --ink-muted: rgba(255,255,255,0.72);
      --ink-faint: rgba(255,255,255,0.18);
      --hairline: #362d59;
      --hairline-strong: rgba(255,255,255,0.14);
      --lime: #c2ef4e;
      --pink: #fa7faa;
      --violet: #6a5fc1;
      --violet-deep: #422082;
      --violet-mid: #79628c;
      --radius-sm: 4px;
      --radius-md: 8px;
      --radius-lg: 12px;
      --radius-xl: 18px;
      color-scheme: dark;
      font-family: Rubik, -apple-system, system-ui, "Segoe UI", Helvetica, Arial, sans-serif;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--canvas); color: var(--ink); line-height: 1.5; }
    main { max-width: 1180px; margin: 0 auto; padding: 24px; }
    header { display: flex; justify-content: space-between; gap: 24px; align-items: center; margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--hairline); }
    .brand { display: flex; align-items: baseline; gap: 12px; }
    h1 { margin: 0; font-size: 24px; font-weight: 600; letter-spacing: 0; }
    .eyebrow { font-size: 11px; font-weight: 600; letter-spacing: 0.25px; text-transform: uppercase; color: var(--ink-muted); }
    .status-chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: var(--radius-sm); font-size: 12px; font-weight: 700; letter-spacing: 0.2px; text-transform: uppercase; background: var(--lime); color: var(--night); }
    .status-chip.warn { background: var(--pink); color: var(--night); }
    .status-chip.mute { background: var(--violet-mid); color: var(--ink); }
    .window { background: var(--panel); border: 1px solid var(--hairline); border-radius: var(--radius-lg); padding: 16px; }
    h2 { font-size: 12px; font-weight: 700; letter-spacing: 0.2px; text-transform: uppercase; color: var(--ink-muted); margin: 0 0 12px; }
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 16px; }
    .span-3 { grid-column: span 3; }
    .span-6 { grid-column: span 6; }
    .metric { font-size: 30px; font-weight: 700; color: var(--ink); line-height: 1.1; }
    .metric.small { font-size: 14px; font-weight: 500; color: var(--ink-muted); line-height: 1.4; }
    .muted { color: var(--ink-muted); font-size: 13px; }
    .topbar { display: flex; gap: 8px; }
    button { border: 1px solid var(--hairline); background: transparent; border-radius: var(--radius-md); padding: 8px 14px; cursor: pointer; color: var(--ink-muted); font-family: inherit; font-size: 12px; font-weight: 700; letter-spacing: 0.2px; text-transform: uppercase; }
    button[aria-pressed="true"] { background: var(--ink); color: var(--night); border-color: var(--ink); }
    button:hover { border-color: var(--ink-muted); color: var(--ink); }
    table { width: 100%; border-collapse: collapse; font-size: 14px; }
    th, td { border-bottom: 1px solid var(--hairline); padding: 8px; text-align: left; }
    th { font-size: 11px; font-weight: 600; letter-spacing: 0.25px; text-transform: uppercase; color: var(--ink-muted); }
    td { color: var(--ink); }
    td.mono, .mono { font-family: Monaco, Menlo, "Ubuntu Mono", monospace; font-size: 13px; }
    .bar { height: 8px; border-radius: 999px; background: var(--violet-deep); overflow: hidden; }
    .bar > span { display: block; height: 100%; background: var(--lime); }
    .badge { display: inline-block; padding: 2px 6px; border-radius: var(--radius-sm); font-size: 11px; font-weight: 600; letter-spacing: 0.25px; text-transform: uppercase; background: var(--violet-mid); color: var(--ink); }
    .badge.error { background: rgba(250,127,170,0.18); color: var(--pink); }
    .badge.ok { background: rgba(194,239,78,0.18); color: var(--lime); }
    @media (max-width: 900px) { .span-3, .span-6 { grid-column: span 12; } header { align-items: flex-start; flex-direction: column; } }
    @media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }
  </style>
</head>
<body>
  <main>
    <header>
      <div class="brand">
        <h1>AI Gateway</h1>
        <span id="readiness-chip" class="status-chip mute">checking</span>
      </div>
      <div class="topbar">
        <button data-days="1">24h</button>
        <button data-days="7">7d</button>
        <button data-days="30" aria-pressed="true">30d</button>
      </div>
    </header>
    <div class="eyebrow" id="updated">Loading...</div>
    <section class="grid" style="margin-top: 20px;">
      <div class="window span-3"><h2>Readiness</h2><div id="readiness" class="metric">-</div><div id="health-summary" class="metric small" style="margin-top: 8px;"></div></div>
      <div class="window span-3"><h2>Requests</h2><div id="requests" class="metric">-</div></div>
      <div class="window span-3"><h2>Tokens</h2><div id="tokens" class="metric">-</div></div>
      <div class="window span-3"><h2>Est. Spend</h2><div id="spend" class="metric">-</div></div>
      <div class="window span-6"><h2>Top Models</h2><div id="models"></div></div>
      <div class="window span-6"><h2>Daily Usage</h2><div id="daily"></div></div>
      <div class="window span-6"><h2>Provider Availability</h2><div id="availability"></div></div>
      <div class="window span-6"><h2>Recent Failures</h2><div id="failures"></div></div>
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
      const status = live.readiness.status || "unknown";
      const chip = document.getElementById("readiness-chip");
      chip.textContent = status;
      chip.className = "status-chip " + (status === "ready" ? "" : status === "not ready" ? "warn" : "mute");
      document.getElementById("updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
      document.getElementById("readiness").textContent = status;
      const degraded = Object.entries(live.health).filter(([k, v]) => k !== "status" && k !== "router" && v !== "ok" && v !== "disabled");
      document.getElementById("health-summary").textContent = degraded.map(([k, v]) => `${k}: ${v}`).join(" / ") || "all systems ok";
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
        `<span class="mono">${escapeHtml(row.served_model)}</span>`,
        fmt.format(row.requests || 0),
        `${fmt.format(row.total_tokens || 0)}<div class="bar"><span style="width:${Math.round(((row.total_tokens || 0) / max) * 100)}%"></span></div>`,
        usd.format(row.estimated_cost_usd || 0),
      ]));
    }

    function renderDaily(rows) {
      const max = Math.max(1, ...rows.map((row) => row.total_tokens || 0));
      document.getElementById("daily").innerHTML = table(["Day", "Requests", "Tokens"], rows.map((row) => [
        `<span class="mono">${escapeHtml(row.day)}</span>`,
        fmt.format(row.requests || 0),
        `${fmt.format(row.total_tokens || 0)}<div class="bar"><span style="width:${Math.round(((row.total_tokens || 0) / max) * 100)}%"></span></div>`,
      ]));
    }

    function renderAvailability(map) {
      const rows = Object.entries(map).map(([model, value]) => [
        `<span class="mono">${escapeHtml(model)}</span>`,
        fmt.format(value.attempts || 0),
        badge(`${value.availability_percent || 0}%`, (value.availability_percent || 0) >= 90 ? "ok" : (value.availability_percent || 0) >= 50 ? "" : "error"),
      ]);
      document.getElementById("availability").innerHTML = table(["Model", "Attempts", "Availability"], rows);
    }

    function renderFailures(rows) {
      document.getElementById("failures").innerHTML = table(["Model", "Status", "Error", "Fallbacks"], rows.map((row) => [
        `<span class="mono">${escapeHtml(row.served_model)}</span>`,
        `<span class="mono">${badge(escapeHtml(row.status), /^2/.test(row.status) ? "ok" : "error")}</span>`,
        `<span class="mono">${escapeHtml(row.error_class || "-")}</span>`,
        fmt.format(row.fallback_count || 0),
      ]));
    }

    function table(headers, rows) {
      if (!rows.length) return '<div class="muted">No data yet.</div>';
      return `<table><thead><tr>${headers.map((header) => `<th>${header}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${row.map((cell) => `<td>${cell}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
    }

    function badge(text, kind) {
      return `<span class="badge ${kind}">${text}</span>`;
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
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

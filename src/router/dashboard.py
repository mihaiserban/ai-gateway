from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
from psycopg.rows import dict_row


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

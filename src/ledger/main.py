from __future__ import annotations

import asyncio
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
        with psycopg.connect(self.database_url) as conn, conn.cursor() as cur:
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
        await asyncio.to_thread(app.state.repository.record, event)
        return JSONResponse(status_code=202, content={"status": "accepted"})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()

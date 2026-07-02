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
        self._client: httpx.AsyncClient | None = None

    async def record(self, event: UsageEvent) -> None:
        if not self.base_url:
            return
        response = await self._http_client().post(f"{self.base_url}/usage-events", json=asdict(event))
        response.raise_for_status()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(transport=self.transport, timeout=2.0)
        return self._client


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

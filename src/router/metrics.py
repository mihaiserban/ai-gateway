from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class ProviderAvailability:
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    retryable_failures: int = 0
    last_status: int | str | None = None
    last_failure_ts: float | None = None
    served_model: str | None = None

    def snapshot(self) -> dict[str, object]:
        availability_percent = 0.0
        if self.attempts:
            availability_percent = round((self.successes / self.attempts) * 100, 2)
        return {
            "attempts": self.attempts,
            "successes": self.successes,
            "failures": self.failures,
            "retryable_failures": self.retryable_failures,
            "availability_percent": availability_percent,
            "last_status": self.last_status,
            "last_failure_ts": self.last_failure_ts,
            "served_model": self.served_model,
        }


@dataclass
class Metrics:
    """In-memory request counters.

    Per-process and reset on restart; not shared across workers. Good enough
    for a LAN-only debug view of which models the router selects and how often
    fallbacks happen.
    """

    requests_total: int = 0
    fallback_count_total: int = 0
    selected_model_counts: dict[str, int] = field(default_factory=dict)
    served_model_counts: dict[str, int] = field(default_factory=dict)
    cache_counts: dict[str, int] = field(default_factory=lambda: {"hit": 0, "miss": 0, "unknown": 0})
    provider_availability: dict[str, ProviderAvailability] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def record(
        self,
        selected_model: str,
        served_model: str,
        fallback_count: int,
        cache_hit: bool | None = None,
    ) -> None:
        with self._lock:
            self.requests_total += 1
            self.selected_model_counts[selected_model] = self.selected_model_counts.get(selected_model, 0) + 1
            self.served_model_counts[served_model] = self.served_model_counts.get(served_model, 0) + 1
            cache_bucket = "unknown"
            if cache_hit is True:
                cache_bucket = "hit"
            elif cache_hit is False:
                cache_bucket = "miss"
            self.cache_counts[cache_bucket] += 1
            if fallback_count > 0:
                self.fallback_count_total += fallback_count

    def record_provider_attempt(
        self,
        model: str,
        status: int | str,
        *,
        success: bool,
        retryable_failure: bool,
        provider_model: str | None = None,
    ) -> None:
        with self._lock:
            key = provider_model or model
            availability = self.provider_availability.setdefault(key, ProviderAvailability())
            if availability.served_model is None:
                availability.served_model = model
            availability.attempts += 1
            availability.last_status = status
            if success:
                availability.successes += 1
                return
            availability.failures += 1
            availability.last_failure_ts = time.time()
            if retryable_failure:
                availability.retryable_failures += 1

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "requests_total": self.requests_total,
                "fallback_count_total": self.fallback_count_total,
                "selected_model_counts": dict(self.selected_model_counts),
                "served_model_counts": dict(self.served_model_counts),
                "cache_counts": dict(self.cache_counts),
                "provider_availability": {
                    model: availability.snapshot() for model, availability in self.provider_availability.items()
                },
            }

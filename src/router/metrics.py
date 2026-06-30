from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock


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
    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def record(self, model: str, fallback_count: int) -> None:
        with self._lock:
            self.requests_total += 1
            self.selected_model_counts[model] = self.selected_model_counts.get(model, 0) + 1
            if fallback_count > 0:
                self.fallback_count_total += fallback_count

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "requests_total": self.requests_total,
                "fallback_count_total": self.fallback_count_total,
                "selected_model_counts": dict(self.selected_model_counts),
            }
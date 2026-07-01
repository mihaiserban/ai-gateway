"""Process-local in-memory routing state: attempt tracking, health, and scoring.

No Redis, no DB. State is per-process and resets on restart. The scoring
formula combines normalized health, latency, quota, stability,
connection-density, and priority signals into a single score per candidate
deployment. Strategies pick the ordering: ``score`` sorts by score descending
with a stable tie-breaker; ``priority`` sorts by config priority ascending
and pushes quota-cooled deployments to the end.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from router.routing import DeploymentRuntime, ScoringWeights

CALLER_ERROR_STATUSES = {400, 401, 403, 404, 422}
QUOTA_STATUS = {402, 429}
QUOTA_WORDS = ("quota", "rate", "overloaded")


@dataclass
class AttemptToken:
    """Opaque handle returned by :meth:`GatewayRoutingState.start_attempt`.

    Carries the deployment id so ``finish_attempt`` can update the right
    counters without the caller re-asserting it.
    """

    deployment_id: str


@dataclass
class _Health:
    attempts: int = 0
    successes: int = 0
    retryable_failures: int = 0
    last_status: int | str | None = None
    last_failure_ts: float | None = None
    latency_ewma: float | None = None
    quota_cooldown_until: float = 0.0


class GatewayRoutingState:
    """Process-local routing state shared across request handlers.

    Thread-safe via a single lock; the request path is async but the critical
    sections are tiny dict updates.
    """

    def __init__(self, *, quota_cooldown_seconds: int = 300) -> None:
        self.quota_cooldown_seconds = quota_cooldown_seconds
        self._health: dict[str, _Health] = {}
        self._active: dict[str, int] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Attempt tracking
    # ------------------------------------------------------------------
    def start_attempt(self, deployment_id: str) -> AttemptToken:
        with self._lock:
            self._active[deployment_id] = self._active.get(deployment_id, 0) + 1
        return AttemptToken(deployment_id=deployment_id)

    def finish_attempt(self, token: AttemptToken, *, status: int | str, latency_ms: float) -> None:
        dep_id = token.deployment_id
        now = time.time()
        with self._lock:
            active = self._active.get(dep_id, 0)
            if active > 0:
                self._active[dep_id] = active - 1
            health = self._health.setdefault(dep_id, _Health())
            health.last_status = status
            self._update_latency(health, latency_ms)
            self._update_quota(health, status, now)
            self._update_outcome(health, status)

    def record_latency(self, deployment_id: str, latency_ms: float) -> None:
        """Record a latency sample (EWMA: first sample as-is, then 0.2*new + 0.8*prev)."""
        with self._lock:
            health = self._health.setdefault(deployment_id, _Health())
            self._update_latency(health, latency_ms)

    def set_active_for_test(self, deployment_id: str, *, active: int) -> None:
        """Test helper: pin the in-flight count for a deployment."""
        with self._lock:
            if active <= 0:
                self._active.pop(deployment_id, None)
            else:
                self._active[deployment_id] = active

    # ------------------------------------------------------------------
    # Ordering
    # ------------------------------------------------------------------
    def order_deployments(
        self,
        deployment_ids: Sequence[str],
        deployments: Mapping[str, DeploymentRuntime],
        weights: ScoringWeights | None,
        now: float,
    ) -> list[str]:
        """Score and order candidate deployment ids by the score strategy.

        ``weights`` defaults to :class:`ScoringWeights` defaults when ``None``.
        Returns candidates sorted by score descending with a stable
        tie-breaker (original order preserved among equal scores).
        """
        if not deployment_ids:
            return []
        w = weights if weights is not None else ScoringWeights()
        with self._lock:
            latencies: dict[str, float] = {
                dep_id: health.latency_ewma
                for dep_id in deployment_ids
                if (health := self._health.get(dep_id)) is not None and health.latency_ewma is not None
            }
        known_latencies = [latencies[d] for d in deployment_ids if d in latencies]
        slowest: float | None = max(known_latencies) if known_latencies else None
        if slowest is not None and slowest <= 0:
            slowest = None
        priorities = [deployments[d].priority for d in deployment_ids if d in deployments]
        best_priority = min(priorities) if priorities else None
        worst_priority = max(priorities) if priorities else None
        scored = [
            (
                self._score(
                    dep_id,
                    deployments.get(dep_id),
                    w,
                    now,
                    slowest=slowest,
                    best_priority=best_priority,
                    worst_priority=worst_priority,
                ),
                index,
                dep_id,
            )
            for index, dep_id in enumerate(deployment_ids)
        ]
        # Stable sort: Python sort is stable, so equal scores keep input order.
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [dep_id for _, _, dep_id in scored]

    def in_quota_cooldown(self, deployment_id: str, now: float) -> bool:
        with self._lock:
            health = self._health.get(deployment_id)
            if health is None:
                return False
            return health.quota_cooldown_until > now

    # ------------------------------------------------------------------
    # Snapshots for metrics/dashboard
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            deployments: dict[str, Any] = {}
            now = time.time()
            for dep_id, health in self._health.items():
                deployments[dep_id] = {
                    "attempts": health.attempts,
                    "successes": health.successes,
                    "retryable_failures": health.retryable_failures,
                    "last_status": health.last_status,
                    "last_failure_ts": health.last_failure_ts,
                    "latency_ewma_ms": health.latency_ewma,
                    "active": self._active.get(dep_id, 0),
                    "quota_cooldown": health.quota_cooldown_until > now,
                    "quota_cooldown_until": health.quota_cooldown_until or None,
                }
            return {
                "deployments": deployments,
                "quota_cooldown_seconds": self.quota_cooldown_seconds,
            }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _score(
        self,
        dep_id: str,
        deployment: DeploymentRuntime | None,
        weights: ScoringWeights,
        now: float,
        *,
        slowest: float | None,
        best_priority: int | None,
        worst_priority: int | None,
    ) -> float:
        health_score = self._health_score(dep_id)
        latency_score = self._latency_score(dep_id, slowest)
        quota_score = self._quota_score(dep_id, now)
        stability_score = deployment.stability if deployment is not None else 0.8
        density_score = self._density_score(dep_id, deployment)
        priority_score = self._priority_score(deployment, best_priority, worst_priority)
        return (
            weights.health * health_score
            + weights.latency * latency_score
            + weights.quota * quota_score
            + weights.stability * stability_score
            + weights.connection_density * density_score
            + weights.priority * priority_score
        )

    def _health_score(self, dep_id: str) -> float:
        with self._lock:
            health = self._health.get(dep_id)
            if health is None or health.attempts == 0:
                return 1.0
            return health.successes / health.attempts

    def _latency_score(self, dep_id: str, slowest: float | None) -> float:
        with self._lock:
            health = self._health.get(dep_id)
            if health is None or health.latency_ewma is None:
                return 1.0
            latency = health.latency_ewma
        if slowest is None or slowest <= 0:
            return 1.0
        # Inverse relative to slowest: faster is closer to 1.0.
        return 1.0 - (latency / slowest) if latency < slowest else 0.0

    def _quota_score(self, dep_id: str, now: float) -> float:
        with self._lock:
            health = self._health.get(dep_id)
            if health is None:
                return 1.0
            if health.quota_cooldown_until > now:
                return 0.0
            return 1.0

    @staticmethod
    def _priority_score(
        deployment: DeploymentRuntime | None,
        best: int | None,
        worst: int | None,
    ) -> float:
        if deployment is None or best is None or worst is None or worst == best:
            return 1.0
        # Lower priority value is better -> score 1.0 at best, 0.0 at worst.
        return 1.0 - (deployment.priority - best) / (worst - best)

    def _density_score(self, dep_id: str, deployment: DeploymentRuntime | None) -> float:
        with self._lock:
            active = self._active.get(dep_id, 0)
        max_concurrent = deployment.max_concurrent if deployment is not None else None
        if max_concurrent is None or max_concurrent <= 0:
            return 1.0
        return max(0.0, 1.0 - (active / max_concurrent))

    def _update_latency(self, health: _Health, latency_ms: float) -> None:
        if health.latency_ewma is None:
            health.latency_ewma = latency_ms
        else:
            health.latency_ewma = 0.2 * latency_ms + 0.8 * health.latency_ewma

    def _update_quota(self, health: _Health, status: int | str, now: float) -> None:
        if self._is_quota_signal(status):
            health.quota_cooldown_until = now + self.quota_cooldown_seconds

    @staticmethod
    def _is_quota_signal(status: int | str) -> bool:
        if isinstance(status, int):
            return status in QUOTA_STATUS
        lowered = str(status).lower()
        return any(word in lowered for word in QUOTA_WORDS)

    def _update_outcome(self, health: _Health, status: int | str) -> None:
        is_success = isinstance(status, int) and 200 <= status < 300
        is_caller_error = isinstance(status, int) and status in CALLER_ERROR_STATUSES
        if is_success:
            health.attempts += 1
            health.successes += 1
            return
        if is_caller_error:
            return
        health.attempts += 1
        health.retryable_failures += 1
        health.last_failure_ts = time.time()

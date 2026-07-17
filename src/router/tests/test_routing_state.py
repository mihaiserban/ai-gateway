import time
from concurrent.futures import ThreadPoolExecutor

from router.routing import DeploymentRuntime, ScoringWeights
from router.routing_state import GatewayRoutingState


def _deployments() -> dict[str, DeploymentRuntime]:
    return {
        "a": DeploymentRuntime(provider="p", connection="c", model="m"),
        "b": DeploymentRuntime(provider="p", connection="c", model="m"),
    }


weights = ScoringWeights()


def test_recent_quota_status_moves_candidate_last():
    state = GatewayRoutingState(quota_cooldown_seconds=300)
    deployments = _deployments()
    token = state.start_attempt("a")
    state.finish_attempt(token, status=429, latency_ms=10)
    ordered = state.order_deployments(["a", "b"], deployments, weights, now=time.time())
    assert ordered[-1] == "a"


def test_lower_latency_wins_when_health_equal():
    state = GatewayRoutingState()
    deployments = _deployments()
    state.record_latency("a", 900)
    state.record_latency("b", 100)
    assert state.order_deployments(["a", "b"], deployments, weights, now=1000.0)[0] == "b"


def test_full_connection_density_penalizes_candidate():
    state = GatewayRoutingState()
    deployments = _deployments()
    state.set_active_for_test("a", active=8)
    state.set_active_for_test("b", active=0)
    deployments["a"].max_concurrent = 8
    deployments["b"].max_concurrent = 8
    assert state.order_deployments(["a", "b"], deployments, weights, now=1000.0)[0] == "b"


def test_missing_scoring_weights_use_defaults():
    state = GatewayRoutingState()
    deployments = _deployments()
    ordered = state.order_deployments(["a", "b"], deployments, weights=None, now=1000.0)
    assert ordered == ["a", "b"]


def test_concurrent_attempt_updates_do_not_corrupt_counters():
    state = GatewayRoutingState()

    def complete_attempt(_index):
        token = state.start_attempt("a")
        state.finish_attempt(token, status=200, latency_ms=10)

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(complete_attempt, range(500)))

    snapshot = state.snapshot()["deployments"]["a"]
    assert snapshot["active"] == 0
    assert snapshot["attempts"] == 500
    assert snapshot["successes"] == 500
    assert snapshot["retryable_failures"] == 0

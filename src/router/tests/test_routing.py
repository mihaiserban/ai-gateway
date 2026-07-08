from router.config import load_route_config
from router.routing import (
    ComboRuntime,
    DeploymentRuntime,
    RouteConfig,
    ScoringWeights,
    TierRuntime,
    is_retryable_failure,
    resolve_model_request,
)
from router.routing_state import GatewayRoutingState


def _config(simple_route_config_path: str):
    return load_route_config(config_path=simple_route_config_path)


def test_combo_resolves_to_combo_candidates(simple_route_config_path: str):
    config = _config(simple_route_config_path)
    env = {"OLLAMA_API_BASE": "http://ollama", "OLLAMA_API_KEY": "x", "DEEPSEEK_API_KEY": "x"}
    resolved = resolve_model_request("coder", config, GatewayRoutingState(), now=1000.0, env=env)
    assert resolved.kind == "combo"
    assert resolved.ordered_deployments[0].endswith(".kimi-k2.7-code")


def test_registry_model_resolves_to_all_active_deployments(simple_route_config_path: str):
    config = _config(simple_route_config_path)
    env = {
        "OLLAMA_API_BASE": "http://ollama",
        "OLLAMA_API_KEY": "x",
        "OPENCODE_GO_API_BASE": "http://go",
        "OPENCODE_GO_API_KEY": "x",
    }
    resolved = resolve_model_request("kimi-k2.7-code", config, GatewayRoutingState(), now=1000.0, env=env)
    assert resolved.kind == "registry-model"
    assert "ollama-cloud.kimi-k2.7-code" in resolved.ordered_deployments


def test_connection_model_forces_one_deployment(simple_route_config_path: str):
    config = _config(simple_route_config_path)
    env = {"OLLAMA_API_BASE": "http://ollama", "OLLAMA_API_KEY": "x"}
    resolved = resolve_model_request("ollama-cloud.kimi-k2.7-code", config, GatewayRoutingState(), now=1000.0, env=env)
    assert resolved.kind == "connection-model"
    assert resolved.ordered_deployments == ["ollama-cloud.kimi-k2.7-code"]


def test_inactive_deployments_are_not_routing_candidates(simple_route_config_path: str):
    config = _config(simple_route_config_path)
    resolved = resolve_model_request("kimi-k2.7-code", config, GatewayRoutingState(), now=1000.0, env={})
    assert resolved.kind == "unavailable"
    assert resolved.ordered_deployments == []


def test_unknown_explicit_model_does_not_fallback_to_default(simple_route_config_path: str):
    config = _config(simple_route_config_path)
    resolved = resolve_model_request("typo-model", config, GatewayRoutingState(), now=1000.0, env={})
    assert resolved.kind == "not-found"
    assert resolved.ordered_deployments == []


def test_priority_strategy_preserves_configured_order_except_quota_cooldown():
    config = RouteConfig(
        combos={"priority-combo": ComboRuntime(strategy="priority", candidates=("b", "a", "c"))},
        deployments={
            "a": DeploymentRuntime(provider="p", connection="a", model="m", priority=10),
            "b": DeploymentRuntime(provider="p", connection="b", model="m", priority=30),
            "c": DeploymentRuntime(provider="p", connection="c", model="m", priority=20),
        },
    )
    state = GatewayRoutingState()
    token = state.start_attempt("a")
    state.finish_attempt(token, status=429, latency_ms=10)

    resolved = resolve_model_request("priority-combo", config, state, now=1000.0, env={})

    assert resolved.ordered_deployments == ["b", "c", "a"]


def test_retryable_statuses_fallback_to_next_deployment():
    assert is_retryable_failure(429)
    assert is_retryable_failure(503)
    assert is_retryable_failure("transport_error")
    assert is_retryable_failure("provider quota exceeded")


def test_caller_errors_do_not_fallback():
    assert not is_retryable_failure(400)
    assert not is_retryable_failure(401)
    assert not is_retryable_failure(403)
    assert not is_retryable_failure(422)


def test_combo_tier_overrides_candidates():
    config = RouteConfig(
        combos={
            "coder": ComboRuntime(
                strategy="score",
                candidates=("dep-a", "dep-b"),
                tiers={
                    "fast": TierRuntime(
                        id="fast",
                        candidates=("dep-c", "dep-d"),
                        strategy="score",
                    ),
                },
            ),
        },
        deployments={
            "dep-a": DeploymentRuntime(provider="p", connection="c1", model="m"),
            "dep-b": DeploymentRuntime(provider="p", connection="c2", model="m"),
            "dep-c": DeploymentRuntime(provider="p", connection="c3", model="m"),
            "dep-d": DeploymentRuntime(provider="p", connection="c4", model="m"),
        },
    )
    base = resolve_model_request("coder", config, GatewayRoutingState(), now=1000.0, env={})
    tier = resolve_model_request("coder:fast", config, GatewayRoutingState(), now=1000.0, env={})

    assert base.kind == "combo"
    assert set(base.ordered_deployments) == {"dep-a", "dep-b"}
    assert tier.kind == "combo"
    assert set(tier.ordered_deployments) == {"dep-c", "dep-d"}


def test_combo_tier_inherits_candidates_when_not_specified():
    config = RouteConfig(
        combos={
            "coder": ComboRuntime(
                strategy="score",
                candidates=("dep-a", "dep-b"),
                tiers={
                    "cheap": TierRuntime(
                        id="cheap",
                        scoring=ScoringWeights(latency=0.05, health=0.35),
                    ),
                },
            ),
        },
        deployments={
            "dep-a": DeploymentRuntime(provider="p", connection="c1", model="m"),
            "dep-b": DeploymentRuntime(provider="p", connection="c2", model="m"),
        },
    )
    resolved = resolve_model_request("coder:cheap", config, GatewayRoutingState(), now=1000.0, env={})

    assert resolved.kind == "combo"
    assert set(resolved.ordered_deployments) == {"dep-a", "dep-b"}


def test_combo_tier_inherits_strategy_when_not_specified():
    config = RouteConfig(
        combos={
            "coder": ComboRuntime(
                strategy="score",
                candidates=("dep-a", "dep-b"),
                tiers={
                    "fast": TierRuntime(
                        id="fast",
                        candidates=("dep-a", "dep-b"),
                    ),
                },
            ),
        },
        deployments={
            "dep-a": DeploymentRuntime(provider="p", connection="c1", model="m"),
            "dep-b": DeploymentRuntime(provider="p", connection="c2", model="m"),
        },
    )
    base = resolve_model_request("coder", config, GatewayRoutingState(), now=1000.0, env={})
    tier = resolve_model_request("coder:fast", config, GatewayRoutingState(), now=1000.0, env={})

    assert base.kind == "combo"
    assert tier.kind == "combo"
    assert set(tier.ordered_deployments) == set(base.ordered_deployments)


def test_unknown_tier_returns_not_found():
    config = RouteConfig(
        combos={
            "coder": ComboRuntime(
                strategy="score",
                candidates=("dep-a",),
                tiers={
                    "fast": TierRuntime(
                        id="fast",
                        candidates=("dep-a",),
                    ),
                },
            ),
        },
        deployments={
            "dep-a": DeploymentRuntime(provider="p", connection="c1", model="m"),
        },
    )
    resolved = resolve_model_request("coder:unknown", config, GatewayRoutingState(), now=1000.0, env={})

    assert resolved.kind == "not-found"


def test_tier_with_no_active_candidates_returns_unavailable():
    config = RouteConfig(
        combos={
            "coder": ComboRuntime(
                strategy="score",
                candidates=("dep-a",),
                tiers={
                    "fast": TierRuntime(
                        id="fast",
                        candidates=("dep-b",),
                    ),
                },
            ),
        },
        deployments={
            "dep-a": DeploymentRuntime(provider="p", connection="c1", model="m"),
            "dep-b": DeploymentRuntime(provider="p", connection="c2", model="m", required_env=("MISSING_KEY",)),
        },
    )
    resolved = resolve_model_request("coder:fast", config, GatewayRoutingState(), now=1000.0, env={})

    assert resolved.kind == "unavailable"

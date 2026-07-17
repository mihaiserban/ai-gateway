from __future__ import annotations

import math

import pytest

from router.gateway_config import GatewayConfigError, expand_gateway_config


def minimal_config() -> dict:
    return {
        "providers": {
            "ollama": {
                "adapter": "litellm",
                "litellm_model_prefix": "ollama_chat",
                "api_base_env": "OLLAMA_API_BASE",
                "api_key_env": "OLLAMA_API_KEY",
                "registry": {
                    "models": {
                        "kimi-k2.7-code": {
                            "display_name": "Kimi K2.7 Code",
                            "capabilities": ["chat", "coding"],
                            "context_length": 128000,
                        }
                    }
                },
            },
            "deepseek": {
                "adapter": "litellm",
                "litellm_model_prefix": "deepseek",
                "api_key_env": "DEEPSEEK_API_KEY",
                "registry": {
                    "models": {
                        "deepseek-v4-pro": {
                            "display_name": "DeepSeek V4 Pro",
                            "capabilities": ["chat", "coding", "reasoning"],
                        }
                    }
                },
            },
        },
        "connections": {
            "ollama-cloud": {
                "provider": "ollama",
                "enabled": True,
                "priority": 10,
                "max_concurrent": 8,
                "models": "all",
            },
            "deepseek-api": {
                "provider": "deepseek",
                "enabled": True,
                "priority": 30,
                "models": ["deepseek-v4-pro"],
            },
        },
        "combos": {
            "coder": {
                "strategy": "score",
                "candidates": [
                    {"connection": "ollama-cloud", "model": "kimi-k2.7-code"},
                    {"connection": "deepseek-api", "model": "deepseek-v4-pro"},
                ],
            }
        },
    }


def test_expands_active_connections_into_deployments():
    catalog = expand_gateway_config(minimal_config())
    assert sorted(catalog.deployments) == [
        "deepseek-api.deepseek-v4-pro",
        "ollama-cloud.kimi-k2.7-code",
    ]
    assert catalog.deployments["ollama-cloud.kimi-k2.7-code"].litellm_model == ("ollama_chat/kimi-k2.7-code")
    assert catalog.deployments["ollama-cloud.kimi-k2.7-code"].capabilities == (
        "chat",
        "coding",
    )


def test_disabled_connections_do_not_create_deployments():
    config = minimal_config()
    config["connections"]["ollama-cloud"]["enabled"] = False
    catalog = expand_gateway_config(config)
    assert "ollama-cloud.kimi-k2.7-code" not in catalog.deployments


@pytest.mark.parametrize("enabled", [None, 0, 1, "false"])
def test_rejects_non_boolean_connection_enabled(enabled):
    config = minimal_config()
    config["connections"]["ollama-cloud"]["enabled"] = enabled

    with pytest.raises(GatewayConfigError, match="enabled must be a bool"):
        expand_gateway_config(config)


@pytest.mark.parametrize("model_id", [None, 1, ""])
def test_rejects_invalid_registry_model_id(model_id):
    config = minimal_config()
    config["providers"]["ollama"]["registry"]["models"] = {model_id: {}}
    config["connections"] = {}
    config["combos"] = {}

    with pytest.raises(GatewayConfigError, match="model id must be a non-empty string"):
        expand_gateway_config(config)


@pytest.mark.parametrize("context_length", [True, 0, -1])
def test_rejects_invalid_context_length(context_length):
    config = minimal_config()
    model = config["providers"]["ollama"]["registry"]["models"]["kimi-k2.7-code"]
    model["context_length"] = context_length

    with pytest.raises(GatewayConfigError, match="context_length must be a positive int"):
        expand_gateway_config(config)


@pytest.mark.parametrize("cost", [True, -0.01, math.inf, math.nan])
def test_rejects_invalid_model_pricing(cost):
    config = minimal_config()
    model = config["providers"]["ollama"]["registry"]["models"]["kimi-k2.7-code"]
    model["pricing"] = {"input_cost_per_token": cost}

    with pytest.raises(GatewayConfigError, match="input_cost_per_token must be a finite non-negative number"):
        expand_gateway_config(config)


@pytest.mark.parametrize("stability", [-0.01, 1.01, math.inf, math.nan])
def test_rejects_invalid_connection_stability(stability):
    config = minimal_config()
    config["connections"]["ollama-cloud"]["stability"] = stability

    with pytest.raises(GatewayConfigError, match=r"stability must be between 0\.0 and 1\.0"):
        expand_gateway_config(config)


def test_rejects_negative_connection_concurrency():
    config = minimal_config()
    config["connections"]["ollama-cloud"]["max_concurrent"] = -1

    with pytest.raises(GatewayConfigError, match="max_concurrent must be non-negative"):
        expand_gateway_config(config)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cache_ttl_seconds", True),
        ("cache_ttl_seconds", -1),
        ("max_concurrent_upstream", True),
        ("max_concurrent_upstream", -1),
        ("quota_cooldown_seconds", -1),
        ("retry_base_delay", -0.1),
        ("retry_max_delay", math.inf),
    ],
)
def test_rejects_invalid_router_runtime_boundaries(field, value):
    config = minimal_config()
    config["router"] = {field: value}

    with pytest.raises(GatewayConfigError, match=rf"router\.{field}"):
        expand_gateway_config(config)


def test_rejects_combo_candidate_for_missing_connection():
    config = minimal_config()
    config["combos"]["coder"]["candidates"][0]["connection"] = "missing"
    with pytest.raises(GatewayConfigError, match="unknown connection"):
        expand_gateway_config(config)


@pytest.mark.parametrize("in_tier", [False, True])
def test_rejects_duplicate_combo_candidates(in_tier):
    config = minimal_config()
    duplicate = {"connection": "ollama-cloud", "model": "kimi-k2.7-code"}
    if in_tier:
        config["combos"]["coder"]["tiers"] = {"fast": {"candidates": [duplicate, duplicate.copy()]}}
    else:
        config["combos"]["coder"]["candidates"] = [duplicate, duplicate.copy()]

    with pytest.raises(GatewayConfigError, match="duplicate candidate"):
        expand_gateway_config(config)


def test_rejects_combo_registry_model_id_collision():
    config = minimal_config()
    config["combos"]["kimi-k2.7-code"] = config["combos"].pop("coder")
    with pytest.raises(GatewayConfigError, match="collides with registry model"):
        expand_gateway_config(config)


def test_rejects_dot_in_connection_id():
    config = minimal_config()
    config["connections"]["ollama.local"] = config["connections"].pop("ollama-cloud")
    config["combos"]["coder"]["candidates"][0]["connection"] = "ollama.local"
    with pytest.raises(GatewayConfigError, match="connection id"):
        expand_gateway_config(config)


def test_model_id_is_exact_and_not_lowercased():
    config = minimal_config()
    config["providers"]["ollama"]["registry"]["models"] = {
        "Model.With.Case": {"display_name": "Case Model", "capabilities": ["chat"]}
    }
    config["connections"]["ollama-cloud"]["models"] = ["Model.With.Case"]
    config["combos"]["coder"]["candidates"][0]["model"] = "Model.With.Case"
    catalog = expand_gateway_config(config)
    assert "ollama-cloud.Model.With.Case" in catalog.deployments


def test_combo_tiers_are_parsed():
    config = minimal_config()
    config["combos"]["coder"]["tiers"] = {
        "fast": {
            "candidates": [
                {"connection": "ollama-cloud", "model": "kimi-k2.7-code"},
            ],
            "scoring": {"latency": 0.50},
        }
    }
    catalog = expand_gateway_config(config)
    assert "fast" in catalog.combos["coder"].tiers
    tier = catalog.combos["coder"].tiers["fast"]
    assert tier.candidates is not None
    assert len(tier.candidates) == 1
    assert tier.candidates[0].connection_id == "ollama-cloud"
    assert tier.scoring is not None
    assert tier.scoring.latency == 0.50


def test_combo_tier_inherits_from_parent():
    config = minimal_config()
    config["combos"]["coder"]["tiers"] = {
        "fast": {"scoring": {"latency": 0.50}},
    }
    catalog = expand_gateway_config(config)
    tier = catalog.combos["coder"].tiers["fast"]
    assert tier.candidates is None
    assert tier.strategy is None


def test_rejects_tier_reserved_name():
    config = minimal_config()
    config["combos"]["coder"]["tiers"] = {
        "default": {"scoring": {"latency": 0.50}},
    }
    with pytest.raises(GatewayConfigError, match="reserved"):
        expand_gateway_config(config)


def test_rejects_tier_for_unknown_connection():
    config = minimal_config()
    config["combos"]["coder"]["tiers"] = {
        "fast": {
            "candidates": [
                {"connection": "missing", "model": "kimi-k2.7-code"},
            ],
        }
    }
    with pytest.raises(GatewayConfigError, match="unknown connection"):
        expand_gateway_config(config)


def test_tier_with_empty_candidates_is_rejected():
    config = minimal_config()
    config["combos"]["coder"]["tiers"] = {
        "fast": {"candidates": []},
    }
    with pytest.raises(GatewayConfigError, match="non-empty list"):
        expand_gateway_config(config)


def test_router_default_model_with_tier_is_valid():
    config = minimal_config()
    config["router"] = {"default_model": "coder:fast"}
    config["combos"]["coder"]["tiers"] = {
        "fast": {
            "candidates": [
                {"connection": "ollama-cloud", "model": "kimi-k2.7-code"},
            ],
        }
    }
    catalog = expand_gateway_config(config)
    assert catalog.router["default_model"] == "coder:fast"


def test_router_default_model_with_unknown_tier_is_rejected():
    config = minimal_config()
    config["router"] = {"default_model": "coder:unknown"}
    with pytest.raises(GatewayConfigError, match="unknown tier"):
        expand_gateway_config(config)

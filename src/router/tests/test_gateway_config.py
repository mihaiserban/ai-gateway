from __future__ import annotations

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


def test_rejects_combo_candidate_for_missing_connection():
    config = minimal_config()
    config["combos"]["coder"]["candidates"][0]["connection"] = "missing"
    with pytest.raises(GatewayConfigError, match="unknown connection"):
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

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
            "ollama-local": {
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
                    {"connection": "ollama-local", "model": "kimi-k2.7-code"},
                    {"connection": "deepseek-api", "model": "deepseek-v4-pro"},
                ],
            }
        },
    }


def test_expands_active_connections_into_deployments():
    catalog = expand_gateway_config(minimal_config())
    assert sorted(catalog.deployments) == [
        "deepseek-api.deepseek-v4-pro",
        "ollama-local.kimi-k2.7-code",
    ]
    assert catalog.deployments["ollama-local.kimi-k2.7-code"].litellm_model == ("ollama_chat/kimi-k2.7-code")
    assert catalog.deployments["ollama-local.kimi-k2.7-code"].capabilities == (
        "chat",
        "coding",
    )


def test_disabled_connections_do_not_create_deployments():
    config = minimal_config()
    config["connections"]["ollama-local"]["enabled"] = False
    catalog = expand_gateway_config(config)
    assert "ollama-local.kimi-k2.7-code" not in catalog.deployments


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
    config["connections"]["ollama.local"] = config["connections"].pop("ollama-local")
    config["combos"]["coder"]["candidates"][0]["connection"] = "ollama.local"
    with pytest.raises(GatewayConfigError, match="connection id"):
        expand_gateway_config(config)


def test_model_id_is_exact_and_not_lowercased():
    config = minimal_config()
    config["providers"]["ollama"]["registry"]["models"] = {
        "Model.With.Case": {"display_name": "Case Model", "capabilities": ["chat"]}
    }
    config["connections"]["ollama-local"]["models"] = ["Model.With.Case"]
    config["combos"]["coder"]["candidates"][0]["model"] = "Model.With.Case"
    catalog = expand_gateway_config(config)
    assert "ollama-local.Model.With.Case" in catalog.deployments

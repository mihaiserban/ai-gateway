from __future__ import annotations

from pathlib import Path

import pytest

from scripts.generate_configs import (
    generate,
    load_gateway_config,
    render_litellm_config,
    render_router_config,
)

ROOT = Path(__file__).resolve().parents[2]


def test_render_litellm_config_emits_configured_deployment_ids():
    config = load_gateway_config(ROOT / "gateway.config.yaml")

    model_names = [entry["model_name"] for entry in render_litellm_config(config)["model_list"]]

    assert "ollama-local.kimi-k2.7-code" in model_names
    assert "deepseek-api.deepseek-v4-pro" in model_names
    assert "coder" not in model_names


def test_render_litellm_config_drops_none_api_base_for_envless_provider():
    config = load_gateway_config(ROOT / "gateway.config.yaml")

    litellm_config = render_litellm_config(config)
    deepseek_entry = next(
        entry for entry in litellm_config["model_list"] if entry["model_name"] == "deepseek-api.deepseek-v4-pro"
    )

    assert deepseek_entry["litellm_params"]["model"] == "deepseek/deepseek-v4-pro"
    assert deepseek_entry["litellm_params"]["api_key"] == "os.environ/DEEPSEEK_API_KEY"
    assert "api_base" not in deepseek_entry["litellm_params"]
    assert deepseek_entry["model_info"] == {
        "provider": "deepseek",
        "connection": "deepseek-api",
        "model": "deepseek-v4-pro",
    }


def test_render_litellm_config_includes_ollama_api_base_env():
    config = load_gateway_config(ROOT / "gateway.config.yaml")

    litellm_config = render_litellm_config(config)
    ollama_entry = next(
        entry for entry in litellm_config["model_list"] if entry["model_name"] == "ollama-local.kimi-k2.7-code"
    )

    assert ollama_entry["litellm_params"]["model"] == "ollama_chat/kimi-k2.7-code"
    assert ollama_entry["litellm_params"]["api_base"] == "os.environ/OLLAMA_API_BASE"
    assert ollama_entry["litellm_params"]["api_key"] == "os.environ/OLLAMA_API_KEY"
    assert ollama_entry["model_info"] == {
        "provider": "ollama",
        "connection": "ollama-local",
        "model": "kimi-k2.7-code",
    }


def test_render_router_config_uses_deployment_ids_and_combos():
    config = load_gateway_config(ROOT / "gateway.config.yaml")

    router_config = render_router_config(config)

    assert router_config["default_model"] == "coder"
    assert router_config["combos"]["coder"]["candidates"][0] == "ollama-local.kimi-k2.7-code"
    assert router_config["deployments"]["ollama-local.kimi-k2.7-code"]["provider"] == "ollama"
    assert router_config["deployments"]["ollama-local.kimi-k2.7-code"]["connection"] == "ollama-local"
    assert router_config["deployments"]["ollama-local.kimi-k2.7-code"]["model"] == "kimi-k2.7-code"
    assert router_config["deployments"]["ollama-local.kimi-k2.7-code"]["required_env"] == [
        "OLLAMA_API_BASE",
        "OLLAMA_API_KEY",
    ]
    assert "ollama-local.kimi-k2.7-code" in router_config["registry_models"]["kimi-k2.7-code"]


def test_render_router_config_includes_quota_cooldown_and_catalog_default_view():
    config = load_gateway_config(ROOT / "gateway.config.yaml")

    router_config = render_router_config(config)

    assert router_config["quota_cooldown_seconds"] == 300
    assert router_config["catalog"]["default_view"] == "all"


def test_render_router_config_carries_router_runtime_knobs():
    config = load_gateway_config(ROOT / "gateway.config.yaml")

    router_config = render_router_config(config)

    assert router_config["cache_ttl_seconds"] == 600
    assert router_config["retry_base_delay"] == 0.2
    assert router_config["retry_max_delay"] == 2.0
    assert router_config["max_concurrent_upstream"] == 0


def test_render_litellm_config_preserves_litellm_settings():
    config = load_gateway_config(ROOT / "gateway.config.yaml")

    litellm_config = render_litellm_config(config)

    assert litellm_config["litellm_settings"]["cache_params"]["redis_url"] == "os.environ/REDIS_URL"
    assert litellm_config["litellm_settings"]["callbacks"] == config.litellm["logging"]["callbacks"]
    assert litellm_config["general_settings"]["master_key"] == "os.environ/LITELLM_MASTER_KEY"


def test_load_gateway_config_returns_gateway_catalog():
    config = load_gateway_config(ROOT / "gateway.config.yaml")

    from router.gateway_config import GatewayCatalog

    assert isinstance(config, GatewayCatalog)
    assert "ollama-local.kimi-k2.7-code" in config.deployments


def test_generate_rejects_missing_file(tmp_path):
    missing = tmp_path / "nope.yaml"
    with pytest.raises(FileNotFoundError):
        load_gateway_config(missing)


def test_committed_generated_configs_match_gateway_config(tmp_path):
    generate(
        config_path=ROOT / "gateway.config.yaml",
        router_path=tmp_path / "router_config.yaml",
        litellm_path=tmp_path / "litellm.config.yaml",
    )

    assert (tmp_path / "router_config.yaml").read_text(encoding="utf-8") == (
        ROOT / "router" / "router_config.yaml"
    ).read_text(encoding="utf-8")
    assert (tmp_path / "litellm.config.yaml").read_text(encoding="utf-8") == (ROOT / "litellm.config.yaml").read_text(
        encoding="utf-8"
    )

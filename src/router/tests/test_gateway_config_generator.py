from __future__ import annotations

from pathlib import Path

import pytest

from scripts.generate_configs import (
    ConfigError,
    generate,
    load_gateway_config,
    render_litellm_config,
    render_router_config,
)

ROOT = Path(__file__).resolve().parents[2]


def test_render_router_config_from_gateway_config():
    config = load_gateway_config(ROOT / "gateway.config.yaml")
    models = config["models"]

    router_config = render_router_config(config)

    assert router_config["cache_ttl_seconds"] == 600
    assert router_config["retry_base_delay"] == 0.2
    assert router_config["retry_max_delay"] == 2.0
    assert router_config["default_model"] == "coder"
    assert router_config["allowed_models"] == [model["name"] for model in models]
    assert router_config["fallbacks"] == {
        model["name"]: list(model.get("fallbacks") or []) for model in models
    }
    assert router_config["timeouts"] == {
        model["name"]: model.get("timeout", 120) for model in models
    }
    assert router_config["cache_key_aliases"] == []
    assert router_config["provider_models"] == {
        model["name"]: model["litellm_model"] for model in models
    }


def test_render_litellm_config_from_gateway_config():
    config = load_gateway_config(ROOT / "gateway.config.yaml")
    models = config["models"]

    litellm_config = render_litellm_config(config)

    first_entry = litellm_config["model_list"][0]
    first_cfg = models[0]
    assert first_entry["model_name"] == first_cfg["name"]
    assert first_entry["litellm_params"]["model"] == first_cfg["litellm_model"]
    assert first_entry["litellm_params"]["api_key"] == f"os.environ/{first_cfg['api_key_env']}"
    if "api_base_env" in first_cfg:
        assert first_entry["litellm_params"]["api_base"] == f"os.environ/{first_cfg['api_base_env']}"

    # Spot-check a model that carries additional_drop_params.
    model_with_drop_params = next(
        m for m in litellm_config["model_list"] if "additional_drop_params" in m["litellm_params"]
    )
    assert model_with_drop_params["litellm_params"]["additional_drop_params"] == ["reasoningSummary"]
    assert litellm_config["litellm_settings"]["cache_params"]["redis_url"] == "os.environ/REDIS_URL"
    assert litellm_config["litellm_settings"]["callbacks"] == config["litellm"]["logging"]["callbacks"]
    assert litellm_config["general_settings"]["master_key"] == "os.environ/LITELLM_MASTER_KEY"
    assert litellm_config["router_settings"]["fallbacks"] == [
        {model["name"]: list(model.get("fallbacks") or [])} for model in models
    ]


def test_validation_rejects_unknown_fallback_target(tmp_path):
    config_path = tmp_path / "gateway.config.yaml"
    config_path.write_text(
        """
router:
  cache_ttl_seconds: 600
  default_model: coder
  retry_base_delay: 0.2
  retry_max_delay: 2.0
  cache_key_aliases: []
litellm:
  settings:
    drop_params: true
    request_timeout: 120
    num_retries: 1
  cache:
    type: redis
    redis_url_env: REDIS_URL
  general:
    master_key_env: LITELLM_MASTER_KEY
    database_url_env: DATABASE_URL
models:
  - name: explorer
    litellm_model: ollama_chat/deepseek-v4-flash
    api_key_env: OLLAMA_API_KEY
    api_base_env: OLLAMA_API_BASE
    timeout: 60
    fallbacks:
      - missing
    model_info:
      input_cost_per_token: 0.0
      output_cost_per_token: 0.0
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="fallback target 'missing'"):
        load_gateway_config(config_path)


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

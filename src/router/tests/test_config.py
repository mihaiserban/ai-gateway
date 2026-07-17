from __future__ import annotations

import logging
from pathlib import Path

import pytest

from router import config as config_mod
from router.routing import RouteConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: str) -> Path:
    path.write_text(data)
    return path


def _write_litellm(path: Path, model_names: list[str]) -> Path:
    lines = ["model_list:"]
    for name in model_names:
        lines.append(f"  - model_name: {name}")
        lines.append("    litellm_params:")
        lines.append("      model: provider/fake")
    path.write_text("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_load_route_config_from_yaml_populates_fields(tmp_path):
    cfg_path = _write_yaml(
        tmp_path / "router_config.yaml",
        """
cache_ttl_seconds: 300
default_model: coder
quota_cooldown_seconds: 60
combos:
  coder:
    strategy: score
    candidates:
      - ollama-cloud.kimi-k2.7-code
deployments:
  ollama-cloud.kimi-k2.7-code:
    provider: ollama
    connection: ollama-cloud
    model: kimi-k2.7-code
    required_env:
      - OLLAMA_API_BASE
      - OLLAMA_API_KEY
registry_models:
  kimi-k2.7-code:
    - ollama-cloud.kimi-k2.7-code
""",
    )

    config = config_mod.load_route_config(config_path=str(cfg_path))

    assert isinstance(config, RouteConfig)
    assert config.cache_ttl_seconds == 300
    assert config.default_model == "coder"
    assert config.quota_cooldown_seconds == 60
    assert config.combos["coder"].candidates == ("ollama-cloud.kimi-k2.7-code",)
    assert config.deployments["ollama-cloud.kimi-k2.7-code"].provider == "ollama"
    assert config.deployments["ollama-cloud.kimi-k2.7-code"].required_env == (
        "OLLAMA_API_BASE",
        "OLLAMA_API_KEY",
    )
    assert config.registry_models == {"kimi-k2.7-code": ["ollama-cloud.kimi-k2.7-code"]}


def test_load_route_config_missing_file_falls_back_to_defaults(tmp_path):
    missing = tmp_path / "does_not_exist.yaml"

    config = config_mod.load_route_config(config_path=str(missing))

    assert isinstance(config, RouteConfig)
    assert config.cache_ttl_seconds == 600
    assert config.default_model == "coder"
    assert config.quota_cooldown_seconds == 300
    assert config.catalog_default_view == "all"
    assert config.combos == {}
    assert config.deployments == {}
    assert config.registry_models == {}


def test_load_route_config_respects_env_var(tmp_path, monkeypatch):
    cfg_path = _write_yaml(
        tmp_path / "custom.yaml",
        """
cache_ttl_seconds: 42
default_model: coder
combos:
  coder:
    strategy: score
    candidates: []
""",
    )
    monkeypatch.setenv("ROUTER_CONFIG_PATH", str(cfg_path))

    config = config_mod.load_route_config()

    assert config.cache_ttl_seconds == 42


def test_load_route_config_applies_catalog_default_view(tmp_path):
    cfg_path = _write_yaml(
        tmp_path / "router_config.yaml",
        """
default_model: coder
catalog:
  default_view: internal
""",
    )

    config = config_mod.load_route_config(config_path=str(cfg_path))

    assert config.catalog_default_view == "internal"


@pytest.mark.parametrize("content", ["- not-a-mapping\n", "42\n"])
def test_load_route_config_rejects_non_mapping_yaml(tmp_path, content):
    cfg_path = _write_yaml(tmp_path / "router_config.yaml", content)

    with pytest.raises(config_mod.ConfigValidationError, match="router config must be a mapping"):
        config_mod.load_route_config(config_path=str(cfg_path))


# ---------------------------------------------------------------------------
# LiteLLM cross-check
# ---------------------------------------------------------------------------


def _good_yaml(tmp_path) -> Path:
    return _write_yaml(
        tmp_path / "router_config.yaml",
        """
default_model: coder
combos:
  coder:
    strategy: score
    candidates:
      - ollama-cloud.kimi-k2.7-code
deployments:
  ollama-cloud.kimi-k2.7-code:
    provider: ollama
    connection: ollama-cloud
    model: kimi-k2.7-code
    required_env:
      - OLLAMA_API_BASE
      - OLLAMA_API_KEY
registry_models:
  kimi-k2.7-code:
    - ollama-cloud.kimi-k2.7-code
""",
    )


def test_cross_check_ok_when_deployments_in_litellm(tmp_path):
    cfg_path = _good_yaml(tmp_path)
    config = config_mod.load_route_config(config_path=str(cfg_path))
    litellm_path = _write_litellm(
        tmp_path / "litellm.config.yaml",
        ["ollama-cloud.kimi-k2.7-code"],
    )

    config_mod.cross_check_litellm(config, litellm_path=str(litellm_path))


def test_cross_check_fails_when_deployment_missing_from_litellm(tmp_path):
    cfg_path = _good_yaml(tmp_path)
    config = config_mod.load_route_config(config_path=str(cfg_path))
    litellm_path = _write_litellm(
        tmp_path / "litellm.config.yaml",
        [],  # missing the deployment
    )

    with pytest.raises(config_mod.ConfigValidationError) as exc:
        config_mod.cross_check_litellm(config, litellm_path=str(litellm_path))

    assert "ollama-cloud.kimi-k2.7-code" in str(exc.value)


def test_cross_check_warns_but_does_not_crash_when_litellm_missing(tmp_path, caplog):
    cfg_path = _good_yaml(tmp_path)
    config = config_mod.load_route_config(config_path=str(cfg_path))
    missing = tmp_path / "nope.yaml"

    with caplog.at_level(logging.WARNING, logger="router.config"):
        config_mod.cross_check_litellm(config, litellm_path=str(missing))

    assert any("litellm" in rec.message.lower() for rec in caplog.records)


def test_cross_check_rejects_non_mapping_litellm_yaml(tmp_path):
    config = config_mod.load_route_config(config_path=str(_good_yaml(tmp_path)))
    litellm_path = _write_yaml(tmp_path / "litellm.config.yaml", "- not-a-mapping\n")

    with pytest.raises(config_mod.ConfigValidationError, match="litellm config must be a mapping"):
        config_mod.cross_check_litellm(config, litellm_path=str(litellm_path))

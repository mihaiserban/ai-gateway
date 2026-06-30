from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from router.routing import RouteConfig, DEFAULT_ALLOWED_MODELS, DEFAULT_FALLBACKS
from router import config as config_mod


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
allowed_models:
  - fast
  - deepseek-pro
fallbacks:
  fast:
    - deepseek-pro
timeouts:
  fast: 30
  deepseek-pro: 60
classifier_keywords:
  code_signals:
    - " refactor"
  reasoning_signals:
    - " analyze"
""",
    )

    config = config_mod.load_route_config(config_path=str(cfg_path))

    assert isinstance(config, RouteConfig)
    assert config.cache_ttl_seconds == 300
    assert config.allowed_models == {"fast", "deepseek-pro"}
    assert config.fallbacks == {"fast": ["deepseek-pro"]}
    assert config.timeouts == {"fast": 30, "deepseek-pro": 60}
    assert config.classifier_keywords == {
        "code_signals": [" refactor"],
        "reasoning_signals": [" analyze"],
    }


def test_load_route_config_missing_file_falls_back_to_defaults(tmp_path):
    missing = tmp_path / "does_not_exist.yaml"

    config = config_mod.load_route_config(config_path=str(missing))

    assert isinstance(config, RouteConfig)
    assert config.cache_ttl_seconds == 600
    assert config.allowed_models == set(DEFAULT_ALLOWED_MODELS)
    assert config.fallbacks == dict(DEFAULT_FALLBACKS)
    assert config.timeouts == {}
    assert config.classifier_keywords == {}


def test_load_route_config_respects_env_var(tmp_path, monkeypatch):
    cfg_path = _write_yaml(
        tmp_path / "custom.yaml",
        """
cache_ttl_seconds: 42
allowed_models:
  - only-one
""",
    )
    monkeypatch.setenv("ROUTER_CONFIG_PATH", str(cfg_path))

    config = config_mod.load_route_config()

    assert config.cache_ttl_seconds == 42
    assert config.allowed_models == {"only-one"}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _good_yaml(tmp_path, extra: str = "") -> Path:
    return _write_yaml(
        tmp_path / "router_config.yaml",
        f"""
cache_ttl_seconds: 600
allowed_models:
  - fast
  - deepseek-pro
  - opencodego-code
fallbacks:
  fast:
    - deepseek-pro
  deepseek-pro:
    - opencodego-code
    - fast
{extra}
""",
    )


def test_validate_ok_for_consistent_config(tmp_path):
    cfg_path = _good_yaml(tmp_path)
    config = config_mod.load_route_config(config_path=str(cfg_path))
    # Should not raise.
    config_mod.validate_route_config(config)


def test_validate_fails_when_fallback_key_not_in_allowed_models(tmp_path):
    cfg_path = _write_yaml(
        tmp_path / "router_config.yaml",
        """
cache_ttl_seconds: 600
allowed_models:
  - fast
fallbacks:
  ghost:
    - fast
""",
    )
    config = config_mod.load_route_config(config_path=str(cfg_path))

    with pytest.raises(config_mod.ConfigValidationError) as exc:
        config_mod.validate_route_config(config)

    assert "ghost" in str(exc.value)


def test_validate_fails_when_fallback_target_not_in_allowed_models(tmp_path):
    cfg_path = _write_yaml(
        tmp_path / "router_config.yaml",
        """
cache_ttl_seconds: 600
allowed_models:
  - fast
fallbacks:
  fast:
    - unknown-model
""",
    )
    config = config_mod.load_route_config(config_path=str(cfg_path))

    with pytest.raises(config_mod.ConfigValidationError) as exc:
        config_mod.validate_route_config(config)

    assert "unknown-model" in str(exc.value)


# ---------------------------------------------------------------------------
# LiteLLM cross-check
# ---------------------------------------------------------------------------

def test_cross_check_ok_when_allowed_models_in_litellm(tmp_path):
    cfg_path = _good_yaml(tmp_path)
    config = config_mod.load_route_config(config_path=str(cfg_path))
    litellm_path = _write_litellm(
        tmp_path / "litellm.config.yaml",
        ["fast", "deepseek-pro", "opencodego-code"],
    )

    config_mod.cross_check_litellm(config, litellm_path=str(litellm_path))


def test_cross_check_fails_when_allowed_alias_missing_from_litellm(tmp_path):
    cfg_path = _good_yaml(tmp_path)
    config = config_mod.load_route_config(config_path=str(cfg_path))
    litellm_path = _write_litellm(
        tmp_path / "litellm.config.yaml",
        ["fast", "deepseek-pro"],  # missing opencodego-code
    )

    with pytest.raises(config_mod.ConfigValidationError) as exc:
        config_mod.cross_check_litellm(config, litellm_path=str(litellm_path))

    assert "opencodego-code" in str(exc.value)


def test_cross_check_warns_but_does_not_crash_when_litellm_missing(tmp_path, caplog):
    cfg_path = _good_yaml(tmp_path)
    config = config_mod.load_route_config(config_path=str(cfg_path))
    missing = tmp_path / "nope.yaml"

    with caplog.at_level(logging.WARNING, logger="router.config"):
        config_mod.cross_check_litellm(config, litellm_path=str(missing))

    assert any("litellm" in rec.message.lower() for rec in caplog.records)
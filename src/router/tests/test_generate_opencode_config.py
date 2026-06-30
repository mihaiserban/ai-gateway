from __future__ import annotations

import json

from scripts.generate_opencode_config import (
    _format_display_name,
    _titleize,
    generate,
    render_opencode_config,
)


def test_titleize_respects_brand_overrides():
    assert _titleize("deepseek") == "DeepSeek"
    assert _titleize("opencodego") == "OpenCode Go"
    assert _titleize("glm") == "GLM"
    assert _titleize("kimi-k2.7-code") == "Kimi K2.7 Code"


def test_format_display_name_for_task_aliases():
    assert _format_display_name("coder", {"name": "coder"}, {"name": "coder"}) == "Coder (Kimi K2.7)"


def test_render_opencode_config_contains_all_entries():
    config = {
        "router": {"default_model": "coder"},
        "models": [
            {
                "name": "deepseek-v4-pro-ollama",
                "litellm_model": "ollama_chat/deepseek-v4-pro",
                "api_key_env": "OLLAMA_API_KEY",
                "api_base_env": "OLLAMA_API_BASE",
                "timeout": 120,
                "model_info": {
                    "role": "provider-deployment",
                    "family": "deepseek-v4-pro",
                    "provider": "ollama",
                    "reasoning_level": "high",
                },
            }
        ],
        "aliases": [
            {
                "name": "deepseek-v4-pro",
                "target": "deepseek-v4-pro-ollama",
                "model_info": {"role": "model-family", "family": "deepseek-v4-pro", "reasoning_level": "high"},
            },
            {
                "name": "coder",
                "target": "deepseek-v4-pro",
                "model_info": {"role": "task-alias", "task": "build", "reasoning_level": "medium"},
            },
        ],
    }

    rendered = render_opencode_config(config)
    models = rendered["provider"]["gateway"]["models"]

    assert set(models.keys()) == {"deepseek-v4-pro-ollama", "deepseek-v4-pro", "coder"}
    assert models["deepseek-v4-pro-ollama"]["name"] == "DeepSeek V4 Pro (Ollama)"
    assert models["deepseek-v4-pro"]["name"] == "DeepSeek V4 Pro"
    assert models["coder"]["name"] == "Coder (Kimi K2.7)"


def test_generate_preserves_existing_model_metadata(tmp_path):
    config_path = tmp_path / "gateway.config.yaml"
    config_path.write_text(
        "models:\n"
        "  - name: deepseek-v4-pro-ollama\n"
        "    litellm_model: ollama_chat/deepseek-v4-pro\n"
        "    api_key_env: OLLAMA_API_KEY\n"
        "    model_info:\n"
        "      role: provider-deployment\n"
        "      family: deepseek-v4-pro\n"
        "      provider: ollama\n"
        "aliases:\n"
        "  - name: coder\n"
        "    target: deepseek-v4-pro-ollama\n"
        "    model_info:\n"
        "      role: task-alias\n"
        "      task: build\n",
        encoding="utf-8",
    )
    opencode_path = tmp_path / "opencode.json"
    opencode_path.write_text(
        json.dumps(
            {"provider": {"gateway": {"models": {"coder": {"name": "Coder", "options": {"reasoningEffort": "high"}}}}}}
        ),
        encoding="utf-8",
    )

    merged = generate(config_path=config_path, opencode_path=opencode_path, dry_run=True)
    models = merged["provider"]["gateway"]["models"]

    assert models["coder"]["name"] == "Coder"  # preserved
    assert models["coder"]["options"] == {"reasoningEffort": "high"}
    assert models["deepseek-v4-pro-ollama"]["name"] == "DeepSeek V4 Pro (Ollama)"


def test_generate_creates_file_when_missing(tmp_path):
    config_path = tmp_path / "gateway.config.yaml"
    config_path.write_text(
        "router:\n  default_model: coder\nmodels:\n"
        "  - name: deepseek-v4-pro-ollama\n"
        "    litellm_model: ollama_chat/deepseek-v4-pro\n"
        "    api_key_env: OLLAMA_API_KEY\n"
        "    model_info:\n"
        "      role: provider-deployment\n"
        "      family: deepseek-v4-pro\n"
        "      provider: ollama\n"
        "      reasoning_level: high\n"
        "aliases:\n"
        "  - name: coder\n"
        "    target: deepseek-v4-pro-ollama\n"
        "    model_info:\n"
        "      role: task-alias\n"
        "      task: build\n"
        "      reasoning_level: medium\n",
        encoding="utf-8",
    )
    opencode_path = tmp_path / "opencode.json"

    generate(config_path=config_path, opencode_path=opencode_path)

    with opencode_path.open("r", encoding="utf-8") as fh:
        result = json.load(fh)

    models = result["provider"]["gateway"]["models"]
    assert set(models.keys()) == {"deepseek-v4-pro-ollama", "coder"}
    assert models["coder"]["name"] == "Coder (Kimi K2.7)"


def test_generate_dry_run_does_not_write(tmp_path):
    config_path = tmp_path / "gateway.config.yaml"
    config_path.write_text(
        "router:\n  default_model: coder\nmodels:\n"
        "  - name: deepseek-v4-pro-ollama\n"
        "    litellm_model: ollama_chat/deepseek-v4-pro\n"
        "    api_key_env: OLLAMA_API_KEY\n"
        "    model_info:\n"
        "      role: provider-deployment\n"
        "      family: deepseek-v4-pro\n"
        "      provider: ollama\n"
        "      reasoning_level: high\n",
        encoding="utf-8",
    )
    opencode_path = tmp_path / "opencode.json"

    generate(config_path=config_path, opencode_path=opencode_path, dry_run=True)

    assert not opencode_path.exists()

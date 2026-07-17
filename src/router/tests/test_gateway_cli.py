from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import gateway


def test_models_command_prints_full_catalog(capsys):
    exit_code = gateway.main(["models", "--view", "all"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "coder" in out
    assert "kimi-k2.7-code" in out


@pytest.mark.parametrize("dry_run_args", [[], ["--dry-run"]], ids=["default", "explicit"])
def test_setup_dry_run_does_not_write(tmp_path, capsys, dry_run_args):
    target = tmp_path / "opencode.json"
    plugin_dir = tmp_path / "plugins"
    exit_code = gateway.main(
        [
            "setup",
            "opencode",
            "--mode",
            "local-plugin",
            "--catalog",
            "all",
            "--path",
            str(target),
            "--plugin-dir",
            str(plugin_dir),
            *dry_run_args,
        ]
    )
    assert exit_code == 0
    assert not target.exists()
    assert not plugin_dir.exists()
    assert "localhost:4100/v1" in capsys.readouterr().out


def test_opencode_local_plugin_setup_installs_plugin_without_static_models(tmp_path):
    target = tmp_path / "opencode.json"
    plugin_dir = tmp_path / "plugins" / "agent-ai-gateway"
    exit_code = gateway.main(
        [
            "setup",
            "opencode",
            "--mode",
            "local-plugin",
            "--catalog",
            "all",
            "--path",
            str(target),
            "--plugin-dir",
            str(plugin_dir),
            "--apply",
        ]
    )
    assert exit_code == 0
    data = json.loads(target.read_text())
    assert [
        "./plugins/agent-ai-gateway/index.js",
        {
            "providerId": "gateway",
            "displayName": "Agent AI Gateway",
            "baseURL": "http://localhost:4100/v1",
            "apiKey": "{env:VIRTUAL_KEY}",
            "catalog": "all",
            "modelCacheTtl": 300000,
        },
    ] in data["plugin"]
    assert "gateway" not in data.get("provider", {})
    assert (plugin_dir / "index.js").exists()


def test_opencode_static_setup_writes_catalog_snapshot(tmp_path):
    target = tmp_path / "opencode.json"
    exit_code = gateway.main(
        [
            "setup",
            "opencode",
            "--mode",
            "static",
            "--catalog",
            "combos",
            "--path",
            str(target),
            "--apply",
        ]
    )
    assert exit_code == 0
    data = json.loads(target.read_text())
    assert data["provider"]["gateway"]["npm"] == "@ai-sdk/openai-compatible"
    assert data["provider"]["gateway"]["options"]["apiKey"] == "{env:VIRTUAL_KEY}"
    assert "coder" in data["provider"]["gateway"]["models"]
    assert data["provider"]["gateway"]["models"]["coder"]["limit"]["context"] == 128000


def test_setup_apply_writes_backup(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text('{"keep": true}\n')
    exit_code = gateway.main(["setup", "pi", "--catalog", "combos", "--path", str(target), "--apply"])
    assert exit_code == 0
    assert list(tmp_path.glob("settings.json.bak.*"))
    assert "agent-ai-gateway" in target.read_text()


def test_setup_preserves_unrelated_json_keys(tmp_path):
    target = tmp_path / "opencode.json"
    target.write_text('{"theme": "dark", "provider": {"other": {"name": "keep"}}}\n')
    exit_code = gateway.main(
        ["setup", "opencode", "--mode", "static", "--catalog", "all", "--path", str(target), "--apply"]
    )
    assert exit_code == 0
    data = json.loads(target.read_text())
    assert data["theme"] == "dark"
    assert data["provider"]["other"]["name"] == "keep"
    assert "gateway" in data["provider"]


def test_setup_invalid_json_aborts_without_backup(tmp_path, capsys):
    target = tmp_path / "opencode.json"
    target.write_text("{not json")
    exit_code = gateway.main(["setup", "opencode", "--path", str(target), "--apply"])
    assert exit_code == 1
    assert target.read_text() == "{not json"
    assert not list(tmp_path.glob("opencode.json.bak.*"))
    assert "invalid JSON" in capsys.readouterr().err


def test_doctor_opencode_reports_plugin_and_config_status(tmp_path, capsys):
    target = tmp_path / "opencode.json"
    plugin_dir = tmp_path / "plugins" / "agent-ai-gateway"
    gateway.main(
        [
            "setup",
            "opencode",
            "--mode",
            "local-plugin",
            "--path",
            str(target),
            "--plugin-dir",
            str(plugin_dir),
            "--apply",
        ]
    )
    exit_code = gateway.main(["doctor", "opencode", "--path", str(target), "--plugin-dir", str(plugin_dir)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "configured" in out
    assert "local-plugin" in out


def test_generate_command_honors_config_path(monkeypatch, tmp_path, capsys):
    calls: list[Path] = []

    def fake_generate(*, config_path: Path = gateway.DEFAULT_CONFIG_PATH, **_: object) -> None:
        calls.append(config_path)

    monkeypatch.setattr(gateway, "generate_runtime_configs", fake_generate)
    config_path = tmp_path / "gateway.config.yaml"
    config_path.write_text("providers: {}\n", encoding="utf-8")

    exit_code = gateway.main(["--config", str(config_path), "generate"])

    assert exit_code == 0
    assert calls == [config_path]
    assert str(config_path) in capsys.readouterr().out

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

SRC_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = SRC_ROOT / "gateway.config.yaml"
DEFAULT_OPENCODE_CONFIG_PATH = Path.home() / ".config" / "opencode" / "opencode.json"

_PROVIDER = "gateway"

# Human-curated display names for task aliases. Unknown aliases get generated
# names from their family/provider metadata.
_TASK_NAMES: dict[str, str] = {
    "explorer": "Explorer (DS V4 Flash)",
    "planner": "Planner (GLM-5.2)",
    "coder": "Coder (Kimi K2.7)",
    "coder-fast": "Coder Fast (DS V4 Flash)",
    "vision": "Vision (Kimi K2.6)",
}


class ConfigError(ValueError):
    """Raised when gateway.config.yaml cannot produce valid runtime configs."""


def _load_gateway_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}
    if not isinstance(config, dict):
        raise ConfigError("gateway config must be a YAML mapping")
    return config


def _models(config: dict[str, Any]) -> list[dict[str, Any]]:
    models = config.get("models")
    if not isinstance(models, list) or not models:
        raise ConfigError("models must be a non-empty list")
    if not all(isinstance(model, dict) for model in models):
        raise ConfigError("each model must be a mapping")
    return models


def _aliases(config: dict[str, Any]) -> list[dict[str, Any]]:
    aliases = config.get("aliases") or []
    if not isinstance(aliases, list):
        raise ConfigError("aliases must be a list")
    if not all(isinstance(alias, dict) for alias in aliases):
        raise ConfigError("each alias must be a mapping")
    return aliases


def _entries(config: dict[str, Any]) -> list[dict[str, Any]]:
    return _models(config) + _aliases(config)


def _resolve_entry(
    entry: dict[str, Any],
    entries_by_name: dict[str, dict[str, Any]],
    seen: tuple[str, ...] = (),
) -> dict[str, Any]:
    if "litellm_model" in entry:
        return entry

    name = entry["name"]
    target = entry.get("target")
    if not isinstance(target, str) or not target:
        raise ConfigError(f"alias {name!r} is missing required string field 'target'")
    if target not in entries_by_name:
        raise ConfigError(f"alias {name!r} targets unknown entry {target!r}")
    if target in seen:
        cycle = " -> ".join((*seen, target))
        raise ConfigError(f"alias cycle detected: {cycle}")
    return _resolve_entry(entries_by_name[target], entries_by_name, (*seen, target))


_BRAND_OVERRIDES: dict[str, str] = {
    "deepseek": "DeepSeek",
    "opencodego": "OpenCode Go",
    "glm": "GLM",
    "kimi": "Kimi",
    "ollama": "Ollama",
}


def _titleize(value: str) -> str:
    """Turn a slug into a readable title with known brand overrides."""
    return " ".join(
        _BRAND_OVERRIDES.get(part.lower(), part.capitalize())
        for part in value.replace("_", " ").replace("-", " ").split()
    )


def _format_display_name(name: str, entry: dict[str, Any], resolved: dict[str, Any]) -> str:
    if name in _TASK_NAMES:
        return _TASK_NAMES[name]

    model_info = entry.get("model_info") or resolved.get("model_info") or {}
    family = model_info.get("family") or ""
    provider = model_info.get("provider") or ""
    role = model_info.get("role") or ""

    if role == "model-family" and family:
        return _titleize(family)

    if role == "provider-deployment" and family and provider:
        return f"{_titleize(family)} ({_titleize(provider)})"

    return _titleize(name.replace("-", " "))


def _build_opencode_models(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries = _entries(config)
    entries_by_name = {entry["name"]: entry for entry in entries}

    models: dict[str, dict[str, Any]] = {}
    for entry in entries:
        name = entry["name"]
        resolved = _resolve_entry(entry, entries_by_name)
        display_name = _format_display_name(name, entry, resolved)
        models[name] = {"name": display_name}
    return models


def render_opencode_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return an OpenCode provider.models snippet for the gateway provider."""
    return {
        "provider": {
            _PROVIDER: {
                "models": _build_opencode_models(config),
            }
        }
    }


def generate(
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    opencode_path: Path = DEFAULT_OPENCODE_CONFIG_PATH,
    dry_run: bool = False,
) -> dict[str, Any]:
    gateway_config = _load_gateway_config(config_path)

    if opencode_path.exists():
        with opencode_path.open("r", encoding="utf-8") as fh:
            opencode_config = json.load(fh)
        if not isinstance(opencode_config, dict):
            raise ConfigError("opencode config must be a JSON object")
    else:
        opencode_config = {}

    merged = dict(opencode_config)
    provider = merged.setdefault("provider", {})
    if not isinstance(provider, dict):
        raise ConfigError("opencode config 'provider' must be a mapping")
    gateway_provider = provider.setdefault(_PROVIDER, {})
    if not isinstance(gateway_provider, dict):
        raise ConfigError(f"opencode config provider.{_PROVIDER} must be a mapping")

    existing_models = gateway_provider.get("models", {})
    if not isinstance(existing_models, dict):
        existing_models = {}

    generated_models = _build_opencode_models(gateway_config)

    # Preserve any manually added metadata (e.g. options, limits) on existing
    # entries while ensuring every gateway alias is present.
    merged_models: dict[str, dict[str, Any]] = {}
    for name, generated in generated_models.items():
        existing = existing_models.get(name, {})
        merged_entry = dict(existing)
        merged_entry.setdefault("name", generated["name"])
        merged_models[name] = merged_entry

    gateway_provider["models"] = merged_models

    if not dry_run:
        opencode_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = opencode_path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        tmp_path.replace(opencode_path)

    return merged


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Sync OpenCode model list from gateway.config.yaml into ~/.config/opencode/opencode.json",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to gateway.config.yaml",
    )
    parser.add_argument(
        "--opencode-config",
        type=Path,
        default=DEFAULT_OPENCODE_CONFIG_PATH,
        help="Path to opencode.json",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the merged models block without writing the file",
    )
    args = parser.parse_args(argv)

    merged = generate(
        config_path=args.config,
        opencode_path=args.opencode_config,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        print(json.dumps(merged["provider"][_PROVIDER]["models"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

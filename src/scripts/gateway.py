"""Local gateway CLI: generate configs, inspect catalog, set up clients.

One front door for the gateway's local-facing operations:

    python3 src/scripts/gateway.py generate
    python3 src/scripts/gateway.py doctor
    python3 src/scripts/gateway.py doctor opencode --path ...
    python3 src/scripts/gateway.py explain coder
    python3 src/scripts/gateway.py models --view all
    python3 src/scripts/gateway.py setup opencode --mode local-plugin --catalog all --apply

Local dry-runs build the live catalog in-process from `src/gateway.config.yaml`,
treating every configured deployment as active so the catalog preview does not
depend on the ambient environment.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Allow running as a standalone script (no PYTHONPATH set) by exposing the
# sibling `router` package on sys.path. Under pytest `pythonpath=["src"]` this
# is a no-op.
_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from router.config import _route_config_from_dict  # noqa: E402
from router.gateway_config import GatewayCatalog, load_gateway_catalog  # noqa: E402
from router.live_catalog import build_live_model_catalog  # noqa: E402
from router.routing import RouteConfig  # noqa: E402
from scripts.generate_configs import DEFAULT_CONFIG_PATH, render_router_config  # noqa: E402
from scripts.generate_configs import generate as generate_runtime_configs  # noqa: E402

PLUGIN_SOURCE = Path(__file__).resolve().parent.parent / "clients" / "opencode_plugin" / "index.js"

DEFAULT_BASE_URL = "http://localhost:4100/v1"
DEFAULT_API_KEY_ENV = "VIRTUAL_KEY"
DEFAULT_CATALOG = "all"
VALID_VIEWS = ("all", "combos", "registry", "connections")
MANAGED_BLOCK_START = "# agent-ai-gateway:start"
MANAGED_BLOCK_END = "# agent-ai-gateway:end"


# --------------------------------------------------------------------------- #
# Catalog loading (local dry-run)
# --------------------------------------------------------------------------- #


def _load_catalog(config_path: Path = DEFAULT_CONFIG_PATH) -> GatewayCatalog:
    return load_gateway_catalog(config_path)


def _route_config_from_catalog(catalog: GatewayCatalog) -> RouteConfig:
    return _route_config_from_dict(render_router_config(catalog))


def _synthesized_env(catalog: GatewayCatalog, route_config: RouteConfig) -> dict[str, str]:
    """Return an env mapping in which every configured deployment is active."""
    env: dict[str, str] = {}
    for provider in catalog.providers.values():
        if provider.api_base_env:
            env.setdefault(provider.api_base_env, "x")
        if provider.api_key_env:
            env.setdefault(provider.api_key_env, "x")
    for dep in route_config.deployments.values():
        for name in dep.required_env:
            env.setdefault(name, "x")
    return env


def _local_catalog_entries(catalog: GatewayCatalog, view: str) -> list[dict[str, Any]]:
    route_config = _route_config_from_catalog(catalog)
    env = _synthesized_env(catalog, route_config)
    return build_live_model_catalog(route_config, None, view=view, env=env)


def _resolve_target_base_url(args: argparse.Namespace, clients: dict[str, Any]) -> str:
    raw = (
        getattr(args, "remote", None) or getattr(args, "base_url", None) or clients.get("base_url") or DEFAULT_BASE_URL
    )
    return _normalize_base_url(raw)


def _normalize_base_url(url: str) -> str:
    url = url.rstrip("/")
    if not url.endswith("/v1"):
        url = url + "/v1"
    return url


def _resolve_api_key_ref(
    args: argparse.Namespace,
    clients: dict[str, Any],
    client_entry: ClientEntry,
) -> str:
    if getattr(args, "api_key", None):
        return str(args.api_key)
    env_name = getattr(args, "api_key_env", None) or clients.get("api_key_env") or DEFAULT_API_KEY_ENV
    if client_entry.supports_env_api_key_ref:
        return f"{{env:{env_name}}}"
    return os.environ.get(env_name, "")


# --------------------------------------------------------------------------- #
# Client registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ClientEntry:
    id: str
    display_name: str
    default_paths: tuple[str, ...]
    config_format: str  # "json" | "toml"
    setup_modes: tuple[str, ...]
    default_setup_mode: str
    supports_env_api_key_ref: bool
    doctor_checks: tuple[str, ...]


CLIENTS: dict[str, ClientEntry] = {
    "opencode": ClientEntry(
        id="opencode",
        display_name="OpenCode",
        default_paths=("~/.config/opencode/opencode.json",),
        config_format="json",
        setup_modes=("local-plugin", "static"),
        default_setup_mode="local-plugin",
        supports_env_api_key_ref=True,
        doctor_checks=("config", "plugin"),
    ),
    "pi": ClientEntry(
        id="pi",
        display_name="Pi",
        default_paths=("~/.config/pi/settings.json",),
        config_format="json",
        setup_modes=("static",),
        default_setup_mode="static",
        supports_env_api_key_ref=False,
        doctor_checks=("config",),
    ),
    "codex": ClientEntry(
        id="codex",
        display_name="Codex",
        default_paths=("~/.codex/config.toml", "~/.codex/codex.toml"),
        config_format="toml",
        setup_modes=("static",),
        default_setup_mode="static",
        supports_env_api_key_ref=False,
        doctor_checks=("config",),
    ),
    "claude-code": ClientEntry(
        id="claude-code",
        display_name="Claude Code",
        default_paths=("~/.claude/settings.json",),
        config_format="json",
        setup_modes=("static",),
        default_setup_mode="static",
        supports_env_api_key_ref=True,
        doctor_checks=("config",),
    ),
}


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def _cmd_generate(args: argparse.Namespace) -> int:
    generate_runtime_configs()
    print("Regenerated runtime configs from src/gateway.config.yaml")
    return 0


def _cmd_models(args: argparse.Namespace) -> int:
    view = args.view
    if view not in VALID_VIEWS:
        print(f"error: unknown view {view!r}; use one of {', '.join(VALID_VIEWS)}", file=sys.stderr)
        return 1
    catalog = _load_catalog(args.config)
    entries = _local_catalog_entries(catalog, view)
    for entry in entries:
        kind = entry["gateway"]["kind"]
        print(f"{entry['id']}\t{kind}")
    return 0


def _cmd_explain(args: argparse.Namespace) -> int:
    model_id = args.model
    catalog = _load_catalog(args.config)
    route_config = _route_config_from_catalog(catalog)

    if model_id in route_config.combos:
        combo = route_config.combos[model_id]
        print(f"{model_id}: combo (strategy={combo.strategy})")
        print("candidates:")
        for cand in combo.candidates:
            print(f"  - {cand}")
        return 0

    if model_id in route_config.registry_models:
        deployments = [d for d in route_config.registry_models[model_id] if d in route_config.deployments]
        print(f"{model_id}: registry-model")
        print("deployments:")
        for dep_id in deployments:
            print(f"  - {dep_id}")
        return 0

    if model_id in route_config.deployments:
        dep = route_config.deployments[model_id]
        print(f"{model_id}: connection-model (provider={dep.provider}, connection={dep.connection})")
        return 0

    print(f"{model_id}: not found", file=sys.stderr)
    return 1


def _cmd_doctor(args: argparse.Namespace) -> int:
    catalog = _load_catalog(args.config)
    if args.client:
        return _doctor_client(args, catalog)
    return _doctor_overview(args, catalog)


def _doctor_overview(args: argparse.Namespace, catalog: GatewayCatalog) -> int:
    route_config = _route_config_from_catalog(catalog)
    missing: list[str] = []
    for dep_id, dep in route_config.deployments.items():
        for name in dep.required_env:
            if name not in os.environ:
                missing.append(f"{dep_id} needs {name}")
    print(f"providers: {len(catalog.providers)}")
    print(f"connections: {len(catalog.connections)}")
    print(f"combos: {len(catalog.combos)}")
    print(f"deployments: {len(catalog.deployments)}")
    print(f"missing env vars: {len(missing)}")
    for line in missing:
        print(f"  - {line}")
    print(f"gateway.config.yaml: {args.config}")
    return 0


def _doctor_client(args: argparse.Namespace, catalog: GatewayCatalog) -> int:
    client_id = args.client
    entry = CLIENTS.get(client_id)
    if entry is None:
        print(f"error: unknown client {client_id!r}", file=sys.stderr)
        return 1

    target = _resolve_target_path(args, entry)
    target_status = "not_configured"
    if target and target.exists():
        target_status = "configured"

    plugin_status = "n/a"
    if "plugin" in entry.doctor_checks:
        plugin_dir = Path(args.plugin_dir) if args.plugin_dir else None
        plugin_status = "installed" if plugin_dir and (plugin_dir / "index.js").exists() else "not_installed"

    print(f"client: {entry.id} ({entry.display_name})")
    print(f"config: {target} -> {target_status}")
    if "plugin" in entry.doctor_checks:
        print(f"plugin: {plugin_status}")

    mode = _resolve_setup_mode(args, entry)
    print(f"mode: {mode}")
    return 0


def _cmd_setup(args: argparse.Namespace) -> int:
    client_id = args.client
    entry = CLIENTS.get(client_id)
    if entry is None:
        print(f"error: unknown client {client_id!r}", file=sys.stderr)
        return 1

    mode = _resolve_setup_mode(args, entry)
    if mode not in entry.setup_modes:
        print(f"error: mode {mode!r} not supported by {client_id!r}; use one of {entry.setup_modes}", file=sys.stderr)
        return 1

    catalog = _load_catalog(args.config)
    clients = dict(catalog.clients)
    base_url = _resolve_target_base_url(args, clients)
    if getattr(args, "remote", None) or getattr(args, "base_url", None):
        print(
            "warning: --remote sets the target base URL; catalog snapshot is "
            "sourced from local config (remote fetch not yet implemented)",
            file=sys.stderr,
        )
    api_key_ref = _resolve_api_key_ref(args, clients, entry)
    view = args.catalog or clients.get("default_catalog") or DEFAULT_CATALOG
    if view not in VALID_VIEWS:
        print(f"error: unknown catalog {view!r}", file=sys.stderr)
        return 1

    target = _resolve_target_path(args, entry)
    if target is None:
        print("error: no target path and no default paths for client", file=sys.stderr)
        return 1

    rendered = _render_client_config(
        entry=entry,
        mode=mode,
        catalog=catalog,
        clients=clients,
        base_url=base_url,
        api_key_ref=api_key_ref,
        view=view,
        args=args,
    )

    if args.apply:
        return _apply_client_config(entry, target, rendered)

    print(f"target: {target}")
    print(f"mode: {mode}")
    print(f"base_url: {base_url}")
    if entry.config_format == "json":
        print(json.dumps(rendered, indent=2))
    else:
        print(rendered)
    return 0


# --------------------------------------------------------------------------- #
# Setup mode + path resolution
# --------------------------------------------------------------------------- #


def _resolve_setup_mode(args: argparse.Namespace, entry: ClientEntry) -> str:
    if getattr(args, "mode", None):
        return args.mode
    if entry.id == "opencode":
        clients = _load_catalog(args.config).clients
        opencode_target = dict(clients.get("targets", {}).get("opencode", {}))
        return opencode_target.get("mode", entry.default_setup_mode)
    return entry.default_setup_mode


def _resolve_target_path(args: argparse.Namespace, entry: ClientEntry) -> Path | None:
    if args.path:
        return Path(args.path)
    if entry.default_paths:
        return Path(os.path.expanduser(entry.default_paths[0]))
    return None


# --------------------------------------------------------------------------- #
# Renderers
# --------------------------------------------------------------------------- #


def _render_client_config(
    *,
    entry: ClientEntry,
    mode: str,
    catalog: GatewayCatalog,
    clients: dict[str, Any],
    base_url: str,
    api_key_ref: str,
    view: str,
    args: argparse.Namespace,
) -> Any:
    targets = dict(clients.get("targets", {}))
    target_cfg = dict(targets.get(entry.id, {}))
    default_model = target_cfg.get("model") or clients.get("default_model") or "coder"

    if entry.id == "opencode":
        return _render_opencode(
            mode=mode,
            catalog=catalog,
            base_url=base_url,
            api_key_ref=api_key_ref,
            view=view,
            target_cfg=target_cfg,
            default_model=default_model,
        )
    if entry.id == "pi":
        return _render_pi(
            catalog=catalog,
            base_url=base_url,
            api_key_ref=api_key_ref,
            view=view,
            default_model=default_model,
        )
    if entry.id == "codex":
        return _render_codex(
            catalog=catalog,
            base_url=base_url,
            api_key_ref=api_key_ref,
            view=view,
            default_model=default_model,
        )
    if entry.id == "claude-code":
        return _render_claude_code(
            catalog=catalog,
            base_url=base_url,
            api_key_ref=api_key_ref,
            default_model=default_model,
        )
    raise ValueError(f"no renderer for client {entry.id!r}")


def _catalog_models_for_view(catalog: GatewayCatalog, view: str) -> dict[str, dict[str, Any]]:
    entries = _local_catalog_entries(catalog, view)
    models: dict[str, dict[str, Any]] = {}
    for entry in entries:
        meta = entry["gateway"]
        display = meta.get("model") or entry["id"]
        models[entry["id"]] = {"name": _display_name(entry["id"], meta, display)}
    return models


def _display_name(model_id: str, meta: dict[str, Any], fallback: str) -> str:
    if meta.get("kind") == "combo":
        return model_id.replace("-", " ").title()
    return fallback


def _render_opencode(
    *,
    mode: str,
    catalog: GatewayCatalog,
    base_url: str,
    api_key_ref: str,
    view: str,
    target_cfg: dict[str, Any],
    default_model: str,
) -> dict[str, Any]:
    provider_id = target_cfg.get("provider_id", "gateway")
    cache_ttl = int(target_cfg.get("model_cache_ttl_ms", 300000))
    small_model = target_cfg.get("small_model")

    if mode == "local-plugin":
        plugin_tuple = [
            "./plugins/agent-ai-gateway/index.js",
            {
                "providerId": provider_id,
                "displayName": "Agent AI Gateway",
                "baseURL": base_url,
                "apiKey": api_key_ref,
                "catalog": view,
                "modelCacheTtl": cache_ttl,
            },
        ]
        return {
            "plugin": [plugin_tuple],
            "model": f"gateway/{default_model}",
            **({"small_model": f"gateway/{small_model}"} if small_model else {}),
        }

    # static
    models = _catalog_models_for_view(catalog, view)
    provider_block: dict[str, Any] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Agent AI Gateway",
        "options": {
            "baseURL": base_url,
            "apiKey": api_key_ref,
        },
        "models": models,
    }
    result: dict[str, Any] = {
        "provider": {provider_id: provider_block},
        "model": f"{provider_id}/{default_model}",
    }
    if small_model:
        result["small_model"] = f"{provider_id}/{small_model}"
    return result


def _render_pi(
    *,
    catalog: GatewayCatalog,
    base_url: str,
    api_key_ref: str,
    view: str,
    default_model: str,
) -> dict[str, Any]:
    block: dict[str, Any] = {
        "baseUrl": base_url,
        "apiKey": api_key_ref or "$VIRTUAL_KEY",
        "model": default_model,
        "_managedBy": "agent-ai-gateway",
    }
    if view in ("all", "combos"):
        block["models"] = _catalog_models_for_view(catalog, view)
    return {"agent-ai-gateway": block}


def _render_codex(
    *,
    catalog: GatewayCatalog,
    base_url: str,
    api_key_ref: str,
    view: str,
    default_model: str,
) -> str:
    models = _catalog_models_for_view(catalog, view)
    model_lines = "\n".join(f"  # - {mid}" for mid in models) if models else "  # (none)"
    key_line = (
        f'api_key = "{api_key_ref}"'
        if api_key_ref
        else "# api_key: pass --api-key or set $VIRTUAL_KEY and export VIRTUAL_KEY=..."
    )
    return (
        f"{MANAGED_BLOCK_START}\n"
        f"[gateway]\n"
        f'base_url = "{base_url}"\n'
        f"{key_line}\n"
        f'model = "{default_model}"\n'
        f"# catalog view: {view}\n"
        f"# available models:\n"
        f"{model_lines}\n"
        f"{MANAGED_BLOCK_END}\n"
    )


def _render_claude_code(
    *,
    catalog: GatewayCatalog,
    base_url: str,
    api_key_ref: str,
    default_model: str,
) -> dict[str, Any]:
    # Claude Code uses ANTHROPIC_BASE_URL without /v1 for the Anthropic surface.
    base = base_url.removesuffix("/v1") if base_url.endswith("/v1") else base_url
    return {
        "agent-ai-gateway": {
            "baseUrl": base,
            "apiKey": api_key_ref or "$VIRTUAL_KEY",
            "model": default_model,
        }
    }


# --------------------------------------------------------------------------- #
# Apply (write) policy
# --------------------------------------------------------------------------- #


def _apply_client_config(entry: ClientEntry, target: Path, rendered: Any) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)

    if entry.config_format == "json":
        return _apply_json(entry, target, rendered)
    return _apply_text(entry, target, rendered)


def _apply_json(entry: ClientEntry, target: Path, rendered: dict[str, Any]) -> int:
    existing: dict[str, Any] = {}
    if target.exists():
        raw = target.read_text(encoding="utf-8")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"error: {target} contains invalid JSON: {exc.msg}", file=sys.stderr)
            return 1
        if not isinstance(parsed, dict):
            print(f"error: {target} top-level JSON is not an object", file=sys.stderr)
            return 1
        existing = parsed
        _write_backup(target)

    merged = _merge_json(entry, existing, rendered)
    target.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")

    # local-plugin mode also installs the plugin file
    if entry.id == "opencode" and "plugin" in rendered:
        _install_plugin(target)
    return 0


def _merge_json(entry: ClientEntry, existing: dict[str, Any], rendered: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in rendered.items():
        if key in ("plugin", "provider"):
            current = merged.get(key)
            if key == "plugin" and isinstance(current, list) and isinstance(value, list):
                merged[key] = _merge_plugin_tuples(current, value)
            elif key == "provider" and isinstance(current, dict) and isinstance(value, dict):
                provider_merged = dict(current)
                provider_merged.update(value)
                merged[key] = provider_merged
            else:
                merged[key] = value
        elif key == "model" or key == "small_model":
            merged[key] = value
        else:
            # Managed block under "agent-ai-gateway" key
            if key == "agent-ai-gateway" and isinstance(value, dict):
                block = dict(existing.get("agent-ai-gateway", {}))
                block.update(value)
                merged["agent-ai-gateway"] = block
            else:
                merged[key] = value
    return merged


def _merge_plugin_tuples(current: list[Any], rendered: list[Any]) -> list[Any]:
    result = [list(item) if isinstance(item, list) else item for item in current]
    for new_tuple in rendered:
        if not isinstance(new_tuple, list) or not new_tuple:
            continue
        plugin_path = new_tuple[0]
        replaced = False
        for i, existing_tuple in enumerate(result):
            if isinstance(existing_tuple, list) and existing_tuple and existing_tuple[0] == plugin_path:
                result[i] = new_tuple
                replaced = True
                break
        if not replaced:
            result.append(new_tuple)
    return result


def _apply_text(entry: ClientEntry, target: Path, rendered: str) -> int:
    existing = ""
    if target.exists():
        existing = target.read_text(encoding="utf-8")
        _write_backup(target)
    updated = _replace_managed_block(existing, rendered)
    target.write_text(updated, encoding="utf-8")
    return 0


def _replace_managed_block(existing: str, new_block: str) -> str:
    pattern = re.compile(
        re.escape(MANAGED_BLOCK_START) + r".*?" + re.escape(MANAGED_BLOCK_END),
        re.DOTALL,
    )
    if pattern.search(existing):
        return pattern.sub(new_block.rstrip("\n"), existing)
    if existing and not existing.endswith("\n"):
        existing = existing + "\n"
    return existing + new_block


def _write_backup(target: Path) -> None:
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = target.with_name(f"{target.name}.bak.{stamp}")
    shutil.copy2(target, backup)


def _install_plugin(opencode_target: Path) -> None:
    if not PLUGIN_SOURCE.exists():
        return
    plugin_dir = opencode_target.parent / "plugins" / "agent-ai-gateway"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PLUGIN_SOURCE, plugin_dir / "index.js")


# --------------------------------------------------------------------------- #
# Argparse
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gateway", description="Local gateway CLI")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to gateway.config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("generate", help="Regenerate runtime configs").set_defaults(func=_cmd_generate)

    models = sub.add_parser("models", help="Print the live model catalog")
    models.add_argument("--view", default=DEFAULT_CATALOG, choices=VALID_VIEWS)
    models.set_defaults(func=_cmd_models)

    explain = sub.add_parser("explain", help="Explain a model id")
    explain.add_argument("model")
    explain.set_defaults(func=_cmd_explain)

    doctor = sub.add_parser("doctor", help="Report gateway/client health")
    doctor.add_argument("client", nargs="?", default=None)
    doctor.add_argument("--path", default=None)
    doctor.add_argument("--plugin-dir", default=None)
    doctor.set_defaults(func=_cmd_doctor)

    setup = sub.add_parser("setup", help="Render and write a client config")
    setup.add_argument("client")
    setup.add_argument("--mode", default=None)
    setup.add_argument("--catalog", default=None, choices=VALID_VIEWS)
    setup.add_argument("--path", default=None)
    setup.add_argument("--plugin-dir", default=None)
    setup.add_argument("--remote", default=None, help="Gateway URL to fetch the catalog from")
    setup.add_argument("--base-url", default=None, dest="base_url")
    setup.add_argument("--api-key", default=None, dest="api_key")
    setup.add_argument("--api-key-env", default=None, dest="api_key_env")
    setup.add_argument("--dry-run", action="store_true")
    setup.add_argument("--apply", action="store_true")
    setup.set_defaults(func=_cmd_setup)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())

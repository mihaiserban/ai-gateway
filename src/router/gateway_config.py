"""Gateway catalog model and validation.

This module parses the human-edited `gateway.config.yaml` into a frozen
`GatewayCatalog` of providers, connections, combos, and the deployments they
expand into. Validation runs eagerly so callers get a single `GatewayConfigError`
for any malformed config rather than partial state at runtime.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

__all__ = [
    "Combo",
    "Connection",
    "Deployment",
    "GatewayCatalog",
    "GatewayConfigError",
    "Provider",
    "ScoringWeights",
    "Tier",
    "expand_gateway_config",
    "load_gateway_catalog",
]


class GatewayConfigError(ValueError):
    """Raised when gateway.config.yaml cannot produce a valid catalog."""


# Identity rules from the plan:
#   provider id, connection id, combo id -> lowercase slug [a-z0-9][a-z0-9_-]*
#   connection id and combo id must not contain '.'
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

_DEFAULT_SCORING = {
    "health": 0.30,
    "latency": 0.20,
    "quota": 0.15,
    "stability": 0.15,
    "connection_density": 0.10,
    "priority": 0.10,
}
_VALID_STRATEGIES = ("score", "priority")


@dataclass(frozen=True)
class ScoringWeights:
    health: float = _DEFAULT_SCORING["health"]
    latency: float = _DEFAULT_SCORING["latency"]
    quota: float = _DEFAULT_SCORING["quota"]
    stability: float = _DEFAULT_SCORING["stability"]
    connection_density: float = _DEFAULT_SCORING["connection_density"]
    priority: float = _DEFAULT_SCORING["priority"]


@dataclass(frozen=True)
class Provider:
    id: str
    adapter: str
    litellm_model_prefix: str
    api_base_env: str | None = None
    api_key_env: str | None = None
    drop_params: tuple[str, ...] = ()
    # Mapping of model id -> frozen model metadata dict. Stored as-is from the
    # registry so model ids stay exact opaque strings (no lowercasing).
    models: dict[str, Mapping[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class Connection:
    id: str
    provider_id: str
    enabled: bool = True
    priority: int = 100
    stability: float = 0.8
    max_concurrent: int | None = None
    # Either the literal string "all" or an explicit list of model ids.
    models: str | tuple[str, ...] = "all"


@dataclass(frozen=True)
class ComboCandidate:
    connection_id: str
    model_id: str


@dataclass(frozen=True)
class Tier:
    id: str
    candidates: tuple[ComboCandidate, ...] | None = None
    strategy: str | None = None
    scoring: ScoringWeights | None = None
    task: str | None = None


@dataclass(frozen=True)
class Combo:
    id: str
    strategy: str
    candidates: tuple[ComboCandidate, ...]
    scoring: ScoringWeights = field(default_factory=ScoringWeights)
    task: str | None = None
    tiers: dict[str, Tier] = field(default_factory=dict)


@dataclass(frozen=True)
class Deployment:
    id: str
    connection_id: str
    provider_id: str
    model_id: str
    upstream: str
    litellm_model: str
    display_name: str | None = None
    capabilities: tuple[str, ...] = ()
    context_length: int | None = None
    api_base_env: str | None = None
    api_key_env: str | None = None
    drop_params: tuple[str, ...] = ()
    priority: int = 100
    stability: float = 0.8
    max_concurrent: int | None = None
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None


@dataclass(frozen=True)
class GatewayCatalog:
    providers: dict[str, Provider]
    connections: dict[str, Connection]
    deployments: dict[str, Deployment]
    combos: dict[str, Combo]
    router: Mapping[str, Any] = field(default_factory=dict)
    clients: Mapping[str, Any] = field(default_factory=dict)
    litellm: Mapping[str, Any] = field(default_factory=dict)


def load_gateway_catalog(path: Path) -> GatewayCatalog:
    """Load and validate a gateway catalog from a YAML file path."""
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise GatewayConfigError("gateway config must be a YAML mapping")
    return expand_gateway_config(data)


def expand_gateway_config(data: Mapping[str, Any]) -> GatewayCatalog:
    """Expand a gateway config mapping into a validated `GatewayCatalog`."""
    if not isinstance(data, Mapping):
        raise GatewayConfigError("gateway config must be a mapping")

    providers_raw = _mapping(data, "providers", required=False)
    connections_raw = _mapping(data, "connections", required=False)
    combos_raw = _mapping(data, "combos", required=False)
    router_raw = _mapping(data, "router", required=False)
    clients_raw = _mapping(data, "clients", required=False)
    litellm_raw = _mapping(data, "litellm", required=False)

    providers = _build_providers(providers_raw)
    connections = _build_connections(connections_raw, providers)
    deployments = _build_deployments(connections, providers)
    combos = _build_combos(combos_raw, connections, deployments, providers)

    _validate_router(router_raw, combos, providers, connections, deployments)

    return GatewayCatalog(
        providers=providers,
        connections=connections,
        deployments=deployments,
        combos=combos,
        router=router_raw,
        clients=clients_raw,
        litellm=litellm_raw,
    )


def _build_providers(raw: Mapping[str, Any]) -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    for pid, body in raw.items():
        if not isinstance(body, Mapping):
            raise GatewayConfigError(f"provider {pid!r} must be a mapping")
        _require_slug(pid, "provider id")
        adapter = _required_str(body, "adapter", f"provider {pid!r}")
        if adapter != "litellm":
            raise GatewayConfigError(f"provider {pid!r} adapter must be 'litellm', got {adapter!r}")
        prefix = _required_str(body, "litellm_model_prefix", f"provider {pid!r}")
        api_base_env = _optional_str(body, "api_base_env", f"provider {pid!r}")
        api_key_env = _optional_str(body, "api_key_env", f"provider {pid!r}")
        drop_params = _str_tuple(body, "drop_params", f"provider {pid!r}")
        registry = _mapping(body, "registry", required=False, label=f"provider {pid!r}")
        models = _build_registry_models(registry, pid)
        providers[pid] = Provider(
            id=pid,
            adapter=adapter,
            litellm_model_prefix=prefix,
            api_base_env=api_base_env,
            api_key_env=api_key_env,
            drop_params=drop_params,
            models=models,
        )
    return providers


def _build_registry_models(registry: Mapping[str, Any], provider_id: str) -> dict[str, Mapping[str, Any]]:
    if not registry:
        return {}
    models_raw = _mapping(registry, "models", required=False, label=f"provider {provider_id!r} registry")
    models: dict[str, Mapping[str, Any]] = {}
    for model_id, model_body in models_raw.items():
        if not isinstance(model_body, Mapping):
            raise GatewayConfigError(f"provider {provider_id!r} registry model {model_id!r} must be a mapping")
        # Validate known metadata fields without forbidding unknown ones.
        caps = model_body.get("capabilities")
        if caps is not None and (not isinstance(caps, list) or not all(isinstance(c, str) for c in caps)):
            raise GatewayConfigError(
                f"provider {provider_id!r} model {model_id!r} capabilities must be a list of strings"
            )
        ctx = model_body.get("context_length")
        if ctx is not None and not isinstance(ctx, int):
            raise GatewayConfigError(f"provider {provider_id!r} model {model_id!r} context_length must be an int")
        pricing = model_body.get("pricing")
        if pricing is not None:
            if not isinstance(pricing, Mapping):
                raise GatewayConfigError(f"provider {provider_id!r} model {model_id!r} pricing must be a mapping")
            for cost_key in ("input_cost_per_token", "output_cost_per_token"):
                cost_val = pricing.get(cost_key)
                if cost_val is not None and not isinstance(cost_val, int | float):
                    raise GatewayConfigError(f"provider {provider_id!r} model {model_id!r} {cost_key} must be numeric")
        models[model_id] = model_body
    return models


def _build_connections(raw: Mapping[str, Any], providers: dict[str, Provider]) -> dict[str, Connection]:
    connections: dict[str, Connection] = {}
    for cid, body in raw.items():
        if not isinstance(body, Mapping):
            raise GatewayConfigError(f"connection {cid!r} must be a mapping")
        _require_slug(cid, "connection id")
        if "." in cid:
            raise GatewayConfigError(f"connection id {cid!r} must not contain '.'")
        pid = _required_str(body, "provider", f"connection {cid!r}")
        if pid not in providers:
            raise GatewayConfigError(f"connection {cid!r} references unknown provider {pid!r}")
        enabled = bool(body.get("enabled", True))
        priority = _int_or(body, "priority", default=100, label=f"connection {cid!r}")
        stability = _float_or(body, "stability", default=0.8, label=f"connection {cid!r}")
        max_concurrent_raw = body.get("max_concurrent")
        max_concurrent: int | None
        if max_concurrent_raw is None:
            max_concurrent = None
        elif isinstance(max_concurrent_raw, int) and not isinstance(max_concurrent_raw, bool):
            max_concurrent = max_concurrent_raw
        else:
            raise GatewayConfigError(f"connection {cid!r} max_concurrent must be an int or omitted")
        models = _connection_models(body, cid, providers[pid])
        connections[cid] = Connection(
            id=cid,
            provider_id=pid,
            enabled=enabled,
            priority=priority,
            stability=stability,
            max_concurrent=max_concurrent,
            models=models,
        )
    return connections


def _connection_models(body: Mapping[str, Any], cid: str, provider: Provider) -> str | tuple[str, ...]:
    raw = body.get("models")
    if raw is None or raw == "all":
        if not provider.models:
            raise GatewayConfigError(
                f"connection {cid!r} uses models: all but provider {provider.id!r} has no registry models"
            )
        return "all"
    if not isinstance(raw, list) or not all(isinstance(m, str) for m in raw):
        raise GatewayConfigError(f"connection {cid!r} models must be 'all' or a list of strings")
    models_tuple = tuple(raw)
    for model_id in models_tuple:
        if model_id not in provider.models:
            raise GatewayConfigError(f"connection {cid!r} references unknown provider model {model_id!r}")
    return models_tuple


def _build_deployments(connections: dict[str, Connection], providers: dict[str, Provider]) -> dict[str, Deployment]:
    deployments: dict[str, Deployment] = {}
    for conn in connections.values():
        if not conn.enabled:
            continue
        provider = providers[conn.provider_id]
        model_ids = tuple(provider.models.keys()) if conn.models == "all" else conn.models
        for model_id in model_ids:
            model_meta = provider.models[model_id]
            deployment = _make_deployment(conn, provider, model_id, model_meta)
            if deployment.id in deployments:
                raise GatewayConfigError(f"deployment id {deployment.id!r} is duplicated")
            deployments[deployment.id] = deployment
    return deployments


def _make_deployment(conn: Connection, provider: Provider, model_id: str, model_meta: Mapping[str, Any]) -> Deployment:
    deployment_id = f"{conn.id}.{model_id}"
    litellm_model = f"{provider.litellm_model_prefix}/{model_id}"
    caps = model_meta.get("capabilities")
    capabilities = tuple(caps) if isinstance(caps, list) else ()
    ctx = model_meta.get("context_length")
    if isinstance(ctx, int) and not isinstance(ctx, bool):
        context_length: int | None = ctx
    else:
        context_length = None
    pricing = model_meta.get("pricing")
    input_cost: float | None = None
    output_cost: float | None = None
    if isinstance(pricing, Mapping):
        in_raw = pricing.get("input_cost_per_token")
        out_raw = pricing.get("output_cost_per_token")
        if isinstance(in_raw, int | float) and not isinstance(in_raw, bool):
            input_cost = float(in_raw)
        if isinstance(out_raw, int | float) and not isinstance(out_raw, bool):
            output_cost = float(out_raw)
    display_name = model_meta.get("display_name")
    if not isinstance(display_name, str):
        display_name = None
    return Deployment(
        id=deployment_id,
        connection_id=conn.id,
        provider_id=provider.id,
        model_id=model_id,
        upstream=provider.litellm_model_prefix,
        litellm_model=litellm_model,
        display_name=display_name,
        capabilities=capabilities,
        context_length=context_length,
        api_base_env=provider.api_base_env,
        api_key_env=provider.api_key_env,
        drop_params=provider.drop_params,
        priority=conn.priority,
        stability=conn.stability,
        max_concurrent=conn.max_concurrent,
        input_cost_per_token=input_cost,
        output_cost_per_token=output_cost,
    )


def _build_combos(
    raw: Mapping[str, Any],
    connections: dict[str, Connection],
    deployments: dict[str, Deployment],
    providers: dict[str, Provider],
) -> dict[str, Combo]:
    combos: dict[str, Combo] = {}
    # Registry model ids share the /v1/models namespace with combo ids.
    registry_model_ids: set[str] = set()
    for provider in providers.values():
        registry_model_ids.update(provider.models.keys())

    for combo_id, body in raw.items():
        if not isinstance(body, Mapping):
            raise GatewayConfigError(f"combo {combo_id!r} must be a mapping")
        # Collision check runs before slug validation so that a combo id that
        # matches a registry model id (which may legally contain dots) reports
        # the collision rather than a slug-format error.
        if combo_id in registry_model_ids:
            raise GatewayConfigError(f"combo id {combo_id!r} collides with registry model id")
        _require_slug(combo_id, "combo id")
        if "." in combo_id:
            raise GatewayConfigError(f"combo id {combo_id!r} must not contain '.'")
        # Combo id must not collide with deployment id prefixes
        # (a combo id starting with "<connection id>." is rejected upstream by
        # the no-dot rule, but also guard against combos that match a deployment
        # prefix before the first dot).
        for deployment_id in deployments:
            if deployment_id.split(".", 1)[0] == combo_id:
                raise GatewayConfigError(f"combo id {combo_id!r} collides with deployment id prefix")

        strategy = _required_str(body, "strategy", f"combo {combo_id!r}")
        if strategy not in _VALID_STRATEGIES:
            raise GatewayConfigError(
                f"combo {combo_id!r} strategy must be one of {_VALID_STRATEGIES}, got {strategy!r}"
            )
        candidates_raw = body.get("candidates")
        if not isinstance(candidates_raw, list) or not candidates_raw:
            raise GatewayConfigError(f"combo {combo_id!r} candidates must be a non-empty list")
        candidates: list[ComboCandidate] = []
        for cand in candidates_raw:
            if not isinstance(cand, Mapping):
                raise GatewayConfigError(f"combo {combo_id!r} candidate must be a mapping")
            cand_conn = _required_str(cand, "connection", f"combo {combo_id!r} candidate")
            cand_model = _required_str(cand, "model", f"combo {combo_id!r} candidate")
            _validate_combo_candidate(combo_id, cand_conn, cand_model, connections, providers)
            candidates.append(ComboCandidate(connection_id=cand_conn, model_id=cand_model))

        scoring = _build_scoring(body.get("scoring"), combo_id)
        task = _optional_str(body, "task", f"combo {combo_id!r}")
        tiers = _build_tiers(body, combo_id, connections, providers)
        combos[combo_id] = Combo(
            id=combo_id,
            strategy=strategy,
            candidates=tuple(candidates),
            scoring=scoring,
            task=task,
            tiers=tiers,
        )
    return combos


def _build_scoring(raw: Any, combo_id: str) -> ScoringWeights:
    if raw is None:
        return ScoringWeights()
    if not isinstance(raw, Mapping):
        raise GatewayConfigError(f"combo {combo_id!r} scoring must be a mapping")
    merged = dict(_DEFAULT_SCORING)
    for key, value in raw.items():
        if key not in _DEFAULT_SCORING:
            raise GatewayConfigError(f"combo {combo_id!r} scoring has unknown weight {key!r}")
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise GatewayConfigError(f"combo {combo_id!r} scoring weight {key!r} must be numeric")
        weight = float(value)
        if not 0.0 <= weight <= 1.0:
            raise GatewayConfigError(f"combo {combo_id!r} scoring weight {key!r} must be between 0.0 and 1.0")
        merged[key] = weight
    return ScoringWeights(
        health=merged["health"],
        latency=merged["latency"],
        quota=merged["quota"],
        stability=merged["stability"],
        connection_density=merged["connection_density"],
        priority=merged["priority"],
    )


def _validate_combo_candidate(
    combo_id: str,
    cand_conn: str,
    cand_model: str,
    connections: dict[str, Connection],
    providers: dict[str, Provider],
) -> None:
    conn = connections.get(cand_conn)
    if conn is None:
        raise GatewayConfigError(f"combo {combo_id!r} candidate references unknown connection {cand_conn!r}")
    provider = providers[conn.provider_id]
    served_models = tuple(provider.models.keys()) if conn.models == "all" else cast("tuple[str, ...]", conn.models)
    if cand_model not in served_models:
        raise GatewayConfigError(
            f"combo {combo_id!r} candidate references connection {cand_conn!r} "
            f"which does not serve model {cand_model!r}"
        )


def _build_tiers(
    body: Mapping[str, Any],
    combo_id: str,
    connections: dict[str, Connection],
    providers: dict[str, Provider],
) -> dict[str, Tier]:
    tiers_raw_val = body.get("tiers")
    if tiers_raw_val is None:
        return {}
    if not isinstance(tiers_raw_val, Mapping):
        raise GatewayConfigError(f"combo {combo_id!r} tiers must be a mapping")
    tiers: dict[str, Tier] = {}
    for tier_id, tier_body in dict(tiers_raw_val).items():
        if not isinstance(tier_body, Mapping):
            raise GatewayConfigError(f"combo {combo_id!r} tier {tier_id!r} must be a mapping")
        _require_slug(tier_id, f"combo {combo_id!r} tier id")
        if tier_id in ("balanced", "default"):
            raise GatewayConfigError(
                f"combo {combo_id!r} tier id {tier_id!r} is reserved; "
                f"the bare combo id (e.g. {combo_id!r}) already represents the default/balanced tier"
            )
        tier_candidates: tuple[ComboCandidate, ...] | None = None
        tier_candidates_raw = tier_body.get("candidates")
        if tier_candidates_raw is not None:
            if not isinstance(tier_candidates_raw, list) or not tier_candidates_raw:
                raise GatewayConfigError(f"combo {combo_id!r} tier {tier_id!r} candidates must be a non-empty list")
            cands: list[ComboCandidate] = []
            for cand in tier_candidates_raw:
                if not isinstance(cand, Mapping):
                    raise GatewayConfigError(f"combo {combo_id!r} tier {tier_id!r} candidate must be a mapping")
                cand_conn = _required_str(cand, "connection", f"combo {combo_id!r} tier {tier_id!r} candidate")
                cand_model = _required_str(cand, "model", f"combo {combo_id!r} tier {tier_id!r} candidate")
                _validate_combo_candidate(combo_id, cand_conn, cand_model, connections, providers)
                cands.append(ComboCandidate(connection_id=cand_conn, model_id=cand_model))
            tier_candidates = tuple(cands)
        tier_strategy = _optional_str(tier_body, "strategy", f"combo {combo_id!r} tier {tier_id!r}")
        if tier_strategy is not None and tier_strategy not in _VALID_STRATEGIES:
            raise GatewayConfigError(
                f"combo {combo_id!r} tier {tier_id!r} strategy must be one of {_VALID_STRATEGIES}, "
                f"got {tier_strategy!r}"
            )
        tier_scoring = _build_scoring(tier_body.get("scoring"), f"{combo_id}:{tier_id}")
        tier_task = _optional_str(tier_body, "task", f"combo {combo_id!r} tier {tier_id!r}")
        tiers[tier_id] = Tier(
            id=tier_id,
            candidates=tier_candidates,
            strategy=tier_strategy,
            scoring=tier_scoring,
            task=tier_task,
        )
    return tiers


def _validate_router(
    router: Mapping[str, Any],
    combos: dict[str, Combo],
    providers: dict[str, Provider],
    connections: dict[str, Connection],
    deployments: dict[str, Deployment],
) -> None:
    default_model = router.get("default_model")
    if default_model is None:
        return
    if not isinstance(default_model, str) or not default_model:
        raise GatewayConfigError("router.default_model must be a non-empty string")
    base_id, tier_id = _parse_model_tier(default_model)
    if base_id in combos:
        if tier_id and tier_id not in combos[base_id].tiers:
            raise GatewayConfigError(
                f"router.default_model {default_model!r} references unknown tier {tier_id!r} on combo {base_id!r}"
            )
        return
    valid_ids: set[str] = set()
    for provider in providers.values():
        valid_ids.update(provider.models.keys())
    for deployment_id in deployments:
        valid_ids.add(deployment_id)
    if default_model not in valid_ids:
        raise GatewayConfigError(
            f"router.default_model {default_model!r} must reference a combo, registry model, or deployment id"
        )


def _parse_model_tier(model: str) -> tuple[str, str | None]:
    if ":" in model:
        base, tier = model.split(":", 1)
        return base, tier
    return model, None


def _require_slug(value: Any, label: str) -> None:
    if not isinstance(value, str) or not _SLUG_RE.match(value):
        raise GatewayConfigError(f"{label} {value!r} must be a lowercase slug matching {_SLUG_RE.pattern}")


def _mapping(data: Mapping[str, Any], key: str, *, required: bool = False, label: str | None = None) -> dict[str, Any]:
    value = data.get(key)
    if value is None:
        if required:
            ctx = f" for {label}" if label else ""
            raise GatewayConfigError(f"missing required mapping {key!r}{ctx}")
        return {}
    if not isinstance(value, Mapping):
        raise GatewayConfigError(f"{key} must be a mapping")
    # Convert to a plain dict so callers can mutate freely.
    return dict(value)


def _required_str(data: Mapping[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise GatewayConfigError(f"{label} is missing required string field {key!r}")
    return value


def _optional_str(data: Mapping[str, Any], key: str, label: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise GatewayConfigError(f"{label} field {key!r} must be a non-empty string")
    return value


def _str_tuple(data: Mapping[str, Any], key: str, label: str) -> tuple[str, ...]:
    value = data.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise GatewayConfigError(f"{label} field {key!r} must be a list of strings")
    return tuple(value)


def _int_or(data: Mapping[str, Any], key: str, *, default: int, label: str) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise GatewayConfigError(f"{label} field {key!r} must be an int")
    return value


def _float_or(data: Mapping[str, Any], key: str, *, default: float, label: str) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise GatewayConfigError(f"{label} field {key!r} must be numeric")
    return float(value)

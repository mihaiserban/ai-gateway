import pytest

from router.config import load_route_config
from router.live_catalog import active_deployment_ids, build_live_model_catalog, deployment_is_active
from router.routing import DeploymentRuntime, RouteConfig


@pytest.fixture
def route_config(simple_route_config_path: str) -> RouteConfig:
    return load_route_config(config_path=simple_route_config_path)


def test_all_view_includes_combos_registry_and_connection_models(route_config):
    env = {"OLLAMA_API_BASE": "http://ollama", "OLLAMA_API_KEY": "x", "DEEPSEEK_API_KEY": "x"}
    ids = [entry["id"] for entry in build_live_model_catalog(route_config, None, view="all", env=env)]
    assert "coder" in ids
    assert "kimi-k2.7-code" in ids
    assert "ollama-local.kimi-k2.7-code" in ids


def test_combo_view_only_includes_combos(route_config):
    env = {"OLLAMA_API_BASE": "http://ollama", "OLLAMA_API_KEY": "x", "DEEPSEEK_API_KEY": "x"}
    entries = build_live_model_catalog(route_config, None, view="combos", env=env)
    assert {entry["gateway"]["kind"] for entry in entries} == {"combo"}


def test_registry_model_groups_active_deployments(route_config):
    env = {
        "OLLAMA_API_BASE": "http://ollama",
        "OLLAMA_API_KEY": "x",
        "OPENCODE_GO_API_BASE": "http://go",
        "OPENCODE_GO_API_KEY": "x",
    }
    entry = next(e for e in build_live_model_catalog(route_config, None, env=env) if e["id"] == "kimi-k2.7-code")
    assert entry["gateway"]["kind"] == "registry-model"
    assert "ollama-local.kimi-k2.7-code" in entry["gateway"]["deployments"]


def test_missing_required_env_hides_deployment(route_config):
    entries = build_live_model_catalog(route_config, None, view="connections", env={})
    ids = [entry["id"] for entry in entries]
    assert "ollama-local.kimi-k2.7-code" not in ids


def test_metadata_aggregation_is_deterministic(route_config):
    env = {
        "OLLAMA_API_BASE": "http://ollama",
        "OLLAMA_API_KEY": "x",
        "OPENCODE_GO_API_BASE": "http://go",
        "OPENCODE_GO_API_KEY": "x",
    }
    entry = next(e for e in build_live_model_catalog(route_config, None, env=env) if e["id"] == "kimi-k2.7-code")
    assert entry["gateway"]["providers"] == ["ollama", "opencode-go"]
    assert entry["gateway"]["connections"] == ["ollama-local", "opencode-go"]
    assert entry["gateway"]["capabilities"] == ["chat", "coding"]


def test_all_catalog_order_is_stable(route_config):
    env = {
        "OLLAMA_API_BASE": "http://ollama",
        "OLLAMA_API_KEY": "x",
        "DEEPSEEK_API_KEY": "x",
        "OPENCODE_GO_API_BASE": "http://go",
        "OPENCODE_GO_API_KEY": "x",
    }
    ids = [entry["id"] for entry in build_live_model_catalog(route_config, None, env=env)]
    assert ids.index("coder") < ids.index("kimi-k2.7-code")
    assert ids.index("kimi-k2.7-code") < ids.index("ollama-local.kimi-k2.7-code")


def test_rejects_unknown_view(route_config):
    with pytest.raises(ValueError, match="Unknown catalog view"):
        build_live_model_catalog(route_config, None, view="unknown", env={})


def test_deployment_is_active_returns_missing_env():
    deployment = DeploymentRuntime(
        provider="ollama",
        connection="ollama-local",
        model="kimi-k2.7-code",
        required_env=("OLLAMA_API_BASE", "OLLAMA_API_KEY"),
    )
    assert deployment_is_active(deployment, {"OLLAMA_API_BASE": "x"}) == (False, ["OLLAMA_API_KEY"])
    assert deployment_is_active(deployment, {"OLLAMA_API_BASE": "x", "OLLAMA_API_KEY": "y"}) == (True, [])


def test_active_deployment_ids_filters_by_env(route_config):
    env = {"OLLAMA_API_BASE": "http://ollama", "OLLAMA_API_KEY": "x"}
    assert active_deployment_ids(route_config, env) == {"ollama-local.kimi-k2.7-code"}

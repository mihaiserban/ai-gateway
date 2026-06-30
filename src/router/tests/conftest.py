import pytest

SIMPLE_ROUTE_CONFIG = """
cache_ttl_seconds: 600
default_model: coder
allowed_models:
  - coder
  - explorer
  - planner
fallbacks:
  coder:
    - explorer
    - planner
  explorer:
    - planner
provider_models:
  coder: provider/coder-model
  explorer: provider/explorer-model
  planner: provider/planner-model
""".lstrip()


@pytest.fixture
def simple_route_config_path(tmp_path):
    path = tmp_path / "router_config.yaml"
    path.write_text(SIMPLE_ROUTE_CONFIG, encoding="utf-8")
    return str(path)

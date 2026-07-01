import pytest

SIMPLE_ROUTE_CONFIG = """
cache_ttl_seconds: 600
default_model: coder
catalog:
  default_view: all
combos:
  coder:
    strategy: score
    task: build
    candidates:
      - ollama-cloud.kimi-k2.7-code
      - deepseek-api.deepseek-v4-pro
      - opencode-go.kimi-k2.7-code
deployments:
  ollama-cloud.kimi-k2.7-code:
    provider: ollama
    connection: ollama-cloud
    model: kimi-k2.7-code
    required_env:
      - OLLAMA_API_BASE
      - OLLAMA_API_KEY
    capabilities:
      - chat
      - coding
    context_length: 128000
  deepseek-api.deepseek-v4-pro:
    provider: deepseek
    connection: deepseek-api
    model: deepseek-v4-pro
    required_env:
      - DEEPSEEK_API_KEY
    capabilities:
      - chat
      - coding
      - reasoning
    context_length: 128000
  opencode-go.kimi-k2.7-code:
    provider: opencode-go
    connection: opencode-go
    model: kimi-k2.7-code
    required_env:
      - OPENCODE_GO_API_BASE
      - OPENCODE_GO_API_KEY
    capabilities:
      - chat
      - coding
    context_length: 128000
registry_models:
  kimi-k2.7-code:
    - ollama-cloud.kimi-k2.7-code
    - opencode-go.kimi-k2.7-code
  deepseek-v4-pro:
    - deepseek-api.deepseek-v4-pro
""".lstrip()


@pytest.fixture
def simple_route_config_path(tmp_path):
    path = tmp_path / "router_config.yaml"
    path.write_text(SIMPLE_ROUTE_CONFIG, encoding="utf-8")
    return str(path)

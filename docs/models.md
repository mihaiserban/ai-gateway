# Model reference

This page documents the model aliases and provider wiring in the personal AI
gateway, plus the live catalogs fetched from each provider.

The canonical machine-readable source of truth for the gateway is
`src/gateway.config.yaml`. After editing it, regenerate runtime configs:

```bash
python3 src/scripts/generate_configs.py
```

## Model contract

The gateway exposes three levels of aliases:

| Level | Examples | Use when |
| --- | --- | --- |
| Task alias | `explorer`, `planner`, `coder`, `coder-fast`, `vision` | A tool or orchestrator wants the gateway's recommended default for a job. |
| Model-family alias | `deepseek-v4-flash`, `deepseek-v4-pro`, `glm-5.2`, `kimi-k2.7-code`, `kimi-k2.6` | A caller wants a specific model family but not a specific provider. |
| Provider deployment alias | `deepseek-v4-pro-ollama`, `deepseek-v4-pro-deepseek`, `kimi-k2.7-code-opencodego` | A caller needs to force or debug one provider deployment. |

`model_info.reasoning_level` is catalog metadata with values `none`, `low`,
`medium`, or `high`. It is not translated into provider-specific request
parameters by the router.

Recommended orchestrator mapping:

| Orchestrator role | Gateway alias | Reasoning level |
| --- | --- | --- |
| Explore/search/simple work | `explorer` | `low` |
| Plan/reason/analyze | `planner` | `high` |
| Build/code | `coder` | `medium` |
| Quick edits/commits | `coder-fast` | `low` |
| Image input | `vision` | `medium` |

## Provider wiring

| Provider | LiteLLM prefix | API base env | API key env | Notes |
| --- | --- | --- | --- | --- |
| Ollama local/cloud | `ollama_chat/*` | `OLLAMA_API_BASE` | `OLLAMA_API_KEY` | Zero-cost, primary path for most aliases |
| DeepSeek | `deepseek/*` | n/a | `DEEPSEEK_API_KEY` | Paid API fallback |
| OpenCode Go | `openai/*` | `OPENCODE_GO_API_BASE` | `OPENCODE_GO_API_KEY` | OpenAI-compatible adapter; drops `reasoningSummary` |

## Live provider catalogs

Fetched from each provider's API on **2026-06-30**. Use these to decide which
models to add or retire.

### Ollama (ollama.com/api/tags)

Endpoint: `https://ollama.com/api/tags`

| Model | Size (bytes) | Updated |
| --- | --- | --- |
| `deepseek-v3.1:671b` | 688,586,727,753 | 2025-11-20 |
| `deepseek-v3.2` | 688,586,727,753 | 2025-12-02 |
| `deepseek-v4-flash` | 140,000,000,000 | 2026-04-24 |
| `deepseek-v4-pro` | 1,600,000,000,000 | 2026-04-24 |
| `devstral-2:123b` | 128,249,391,520 | 2025-12-08 |
| `devstral-small-2:24b` | 51,600,000,000 | 2025-12-09 |
| `gemini-3-flash-preview` | 0 | 2025-12-17 |
| `gemma3:12b` | 24,000,000,000 | 2025-03-12 |
| `gemma3:27b` | 55,000,000,000 | 2025-03-12 |
| `gemma3:4b` | 8,600,000,000 | 2025-03-12 |
| `gemma4:31b` | 62,546,177,752 | 2026-04-02 |
| `glm-4.7` | 696,060,000,000 | 2025-12-22 |
| `glm-5` | 756,162,687,872 | 2026-02-11 |
| `glm-5.1` | 1,507,728,316,928 | 2026-04-07 |
| `glm-5.2` | 0 | 2026-06-16 |
| `gpt-oss:120b` | 65,290,180,781 | 2025-08-05 |
| `gpt-oss:20b` | 13,780,162,412 | 2025-08-05 |
| `kimi-k2.5` | 1,118,481,408,000 | 2026-01-26 |
| `kimi-k2.6` | 595,148,192,736 | 2026-03-31 |
| `kimi-k2.7-code` | 595,148,192,736 | 2026-06-12 |
| `minimax-m2.1` | 230,000,000,000 | 2025-12-20 |
| `minimax-m2.5` | 230,000,000,000 | 2026-02-12 |
| `minimax-m2.7` | 480,836,588,544 | 2026-03-18 |
| `minimax-m3` | 0 | 2026-06-01 |
| `ministral-3:14b` | 15,700,000,000 | 2025-12-02 |
| `ministral-3:3b` | 4,670,000,000 | 2025-12-02 |
| `ministral-3:8b` | 10,400,000,000 | 2025-12-02 |
| `mistral-large-3:675b` | 682,000,000,000 | 2025-12-02 |
| `nemotron-3-nano:30b` | 32,645,090,390 | 2025-12-15 |
| `nemotron-3-super` | 230,500,000,000 | 2026-03-11 |
| `nemotron-3-ultra` | 0 | 2026-06-04 |
| `qwen3-coder-next` | 81,800,000,000 | 2025-02-04 |
| `qwen3-coder:480b` | 510,492,157,952 | 2025-07-22 |
| `qwen3.5:397b` | 397,000,000,000 | 2026-02-16 |
| `rnj-1:8b` | 16,000,000,000 | 2025-12-09 |

### DeepSeek (api.deepseek.com/models)

Endpoint: `https://api.deepseek.com/models`

| Model | Context length | Max output | Cache hit / 1M tokens | Cache miss / 1M tokens | Output / 1M tokens | Concurrency limit |
| --- | --- | --- | --- | --- | --- | --- |
| `deepseek-v4-flash` | 1M | 384K | $0.0028 | $0.14 | $0.28 | 2500 |
| `deepseek-v4-pro` | 1M | 384K | $0.003625 | $0.435 | $0.87 | 500 |

DeepSeek's deprecated aliases: `deepseek-chat` and `deepseek-reasoner` (both
deprecated 2026-07-24 15:59 UTC). `deepseek-chat` maps to non-thinking mode of
`deepseek-v4-flash`; `deepseek-reasoner` maps to thinking mode.

### OpenCode Go (opencode.ai/zen/go/v1/models)

Endpoint: `https://opencode.ai/zen/go/v1/models`

| Model |
| --- |
| `deepseek-v4-flash` |
| `deepseek-v4-pro` |
| `glm-5` |
| `glm-5.1` |
| `glm-5.2` |
| `hy3-preview` |
| `kimi-k2.5` |
| `kimi-k2.6` |
| `kimi-k2.7-code` |
| `minimax-m2.5` |
| `minimax-m2.7` |
| `minimax-m3` |
| `mimo-v2-omni` |
| `mimo-v2-pro` |
| `mimo-v2.5` |
| `mimo-v2.5-pro` |
| `qwen3.5-plus` |
| `qwen3.6-plus` |
| `qwen3.7-max` |
| `qwen3.7-plus` |

OpenCode Go exposes all models under an OpenAI-compatible `/v1/models`
endpoint. The gateway routes them with LiteLLM's `openai/*` provider prefix and
adds `additional_drop_params: [reasoningSummary]` for the aliases that need it.

## Active gateway model aliases

| Alias | Alias level | Reasoning level | Target/provider model | Timeout (s) | Fallbacks |
| --- | --- | --- | --- | --- | --- |
| `deepseek-v4-flash-ollama` | provider-deployment | low | `ollama_chat/deepseek-v4-flash` | 60 | — |
| `deepseek-v4-flash-deepseek` | provider-deployment | low | `deepseek/deepseek-v4-flash` | 60 | — |
| `deepseek-v4-flash-opencodego` | provider-deployment | low | `openai/deepseek-v4-flash` | 60 | — |
| `glm-5.2-ollama` | provider-deployment | high | `ollama_chat/glm-5.2` | 120 | — |
| `glm-5.2-opencodego` | provider-deployment | high | `openai/glm-5.2` | 120 | — |
| `kimi-k2.7-code-ollama` | provider-deployment | medium | `ollama_chat/kimi-k2.7-code` | 120 | — |
| `kimi-k2.7-code-opencodego` | provider-deployment | medium | `openai/kimi-k2.7-code` | 120 | — |
| `deepseek-v4-pro-ollama` | provider-deployment | high | `ollama_chat/deepseek-v4-pro` | 120 | — |
| `deepseek-v4-pro-deepseek` | provider-deployment | high | `deepseek/deepseek-v4-pro` | 120 | — |
| `kimi-k2.6-ollama` | provider-deployment | low | `ollama_chat/kimi-k2.6` | 60 | — |
| `kimi-k2.6-opencodego` | provider-deployment | low | `openai/kimi-k2.6` | 120 | — |
| `deepseek-v4-flash` | model-family | low | `ollama_chat/deepseek-v4-flash` | 60 | `deepseek-v4-flash-deepseek`, `deepseek-v4-flash-opencodego` |
| `glm-5.2` | model-family | high | `ollama_chat/glm-5.2` | 120 | `glm-5.2-opencodego`, `kimi-k2.7-code` |
| `kimi-k2.7-code` | model-family | medium | `ollama_chat/kimi-k2.7-code` | 120 | `kimi-k2.7-code-opencodego`, `deepseek-v4-pro` |
| `deepseek-v4-pro` | model-family | high | `ollama_chat/deepseek-v4-pro` | 120 | `deepseek-v4-pro-deepseek` |
| `kimi-k2.6` | model-family | low | `ollama_chat/kimi-k2.6` | 60 | `kimi-k2.6-opencodego` |
| `explorer` | task-alias | low | `ollama_chat/deepseek-v4-flash` | 60 | `deepseek-v4-flash-deepseek`, `deepseek-v4-flash-opencodego` |
| `planner` | task-alias | high | `ollama_chat/glm-5.2` | 120 | `glm-5.2-opencodego`, `kimi-k2.7-code` |
| `coder` | task-alias | medium | `ollama_chat/kimi-k2.7-code` | 120 | `kimi-k2.7-code-opencodego`, `deepseek-v4-pro`, `deepseek-v4-pro-deepseek` |
| `coder-fast` | task-alias | low | `ollama_chat/deepseek-v4-flash` | 60 | `deepseek-v4-flash-deepseek`, `kimi-k2.6`, `coder` |
| `vision` | task-alias | medium | `ollama_chat/kimi-k2.6` | 120 | `kimi-k2.6-opencodego`, `coder` |

## Fallback chains

```text
explorer
  -> deepseek-v4-flash-deepseek
  -> deepseek-v4-flash-opencodego

planner
  -> glm-5.2-opencodego
  -> kimi-k2.7-code

coder
  -> kimi-k2.7-code-opencodego
  -> deepseek-v4-pro
  -> deepseek-v4-pro-deepseek

coder-fast
  -> deepseek-v4-flash-deepseek
  -> kimi-k2.6
  -> coder

vision
  -> kimi-k2.6-opencodego
  -> coder
```

## Runtime configuration

| Setting | Value | Source in `gateway.config.yaml` |
| --- | --- | --- |
| Default model | `coder` | `router.default_model` |
| Cache TTL | 600 seconds | `router.cache_ttl_seconds` |
| Retry base delay | 0.2 seconds | `router.retry_base_delay` |
| Retry max delay | 2.0 seconds | `router.retry_max_delay` |
| Request timeout | 120 seconds | `litellm.settings.request_timeout` |
| Retries | 3 | `litellm.settings.num_retries` |
| Drop unknown params | `true` | `litellm.settings.drop_params` |
| Cache backend | Redis via `REDIS_URL` | `litellm.cache` |

## Environment variables used by models

| Variable | Required by | Purpose |
| --- | --- | --- |
| `OLLAMA_API_BASE` | All `ollama_chat/*` aliases | Ollama endpoint URL |
| `OLLAMA_API_KEY` | All `ollama_chat/*` aliases | Ollama API key (may be empty for local) |
| `DEEPSEEK_API_KEY` | `explorer-ds`, `coder-dsp-ds` | DeepSeek API key |
| `OPENCODE_GO_API_BASE` | All `openai/*` aliases | OpenCode Go base URL |
| `OPENCODE_GO_API_KEY` | All `openai/*` aliases | OpenCode Go API key |

## Updating this page

To refresh the provider catalog tables, source `src/.env` and run the fetch
script (or call the endpoints directly with `curl`). Replace the tables in the
"Live provider catalogs" section with the new responses.

# Single Gateway Config Design

## Goal

Make `src/gateway.config.yaml` the only human-edited project configuration for model aliases, provider mappings, routing behavior, fallbacks, cache settings, and pricing metadata.

## Current State

The gateway has two runtime config files with overlapping responsibilities:

- `src/router/router_config.yaml` controls router aliases, fallbacks, retry delays, timeouts, classifier keywords, session TTL, and prompt-cache aliases.
- `src/litellm.config.yaml` controls LiteLLM model aliases, provider model IDs, API key/base environment references, pricing metadata, LiteLLM cache settings, general settings, and LiteLLM fallback chains.

Fallback chains are duplicated in both files. Editing models safely requires touching both files and remembering which settings belong where.

## Design

Add `src/gateway.config.yaml` as the committed source of truth. A small generator script reads it and writes the two existing runtime config files:

- `src/router/router_config.yaml`
- `src/litellm.config.yaml`

The router and Docker stack can continue using the existing generated files, keeping runtime behavior simple and preserving the current deployment shape. Generated files should include a header making clear they are not the intended edit point.

## Config Ownership

`src/gateway.config.yaml` owns these human-edited values:

- router session TTL
- router retry base and max delay
- model aliases and their provider model IDs
- provider API key and API base environment variable names
- per-alias request timeouts
- router fallback chains and LiteLLM fallback chains, derived from one fallback map
- classifier keyword lists
- aliases that receive `prompt_cache_key`
- LiteLLM global settings such as `drop_params`, `request_timeout`, `num_retries`, Redis cache type, master key env var, and database URL env var
- model pricing metadata
- provider-specific LiteLLM extras such as `additional_drop_params`

`.env` remains the owner of actual secret values. `src/docker-compose.yml` remains the owner of services, ports, volumes, healthchecks, and env injection. `pyproject.toml`, `.pre-commit-config.yaml`, and CI workflow files remain the owner of development tooling.

## Validation

Generation must fail fast when:

- a fallback key does not match a defined model alias
- a fallback target does not match a defined model alias
- a model is missing a LiteLLM provider model ID
- a model references an API key env var with an empty name

Existing router startup validation remains useful as a second guard because it checks the generated runtime files in the same way production uses them.

## Testing

Add tests for the generator that prove:

- the generated router config contains the same router settings as the source config
- the generated LiteLLM config contains model aliases, provider params, pricing metadata, global LiteLLM settings, and fallbacks
- validation rejects fallback references to unknown aliases
- the committed generated files are in sync with `src/gateway.config.yaml`

## Non-Goals

- Do not move secrets into committed YAML.
- Do not change Docker service topology.
- Do not make the router dynamically read the unified config in production.
- Do not add a new dependency beyond packages already used by the router/test environment.

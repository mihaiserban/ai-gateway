# Synology AI Gateway

This runs a small personal AI gateway stack with Docker:

- Sticky FastAPI router on port `4100`
- LiteLLM proxy on internal port `4000`
- Postgres for LiteLLM virtual keys, spend tracking, and UI state
- Redis for LiteLLM cache state and router session stickiness

Agents talk to the router. The router chooses an alias, keeps warm sessions on
the same alias, redacts obvious secrets, and forwards the request to LiteLLM.
LiteLLM handles provider adapters, virtual keys, caching, fallbacks, and spend
tracking.

## Files

```text
src/
  docker-compose.yml
  litellm.config.yaml
  .env.example
  README.md
  router/
    Dockerfile
    requirements.txt
    main.py
    classifier.py
    redaction.py
    routing.py
    sessions.py
    tests/
```

## Setup

On the NAS, copy this folder somewhere persistent, for example:

```bash
/volume1/docker/ai-gateway
```

Create the secret env file:

```bash
cd /volume1/docker/ai-gateway
cp .env.example .env
chmod 600 .env
```

Edit `.env` and set:

- `LITELLM_MASTER_KEY`
- `LITELLM_SALT_KEY`
- `POSTGRES_PASSWORD`
- `DATABASE_URL` with the same Postgres password
- `REDIS_PASSWORD`
- `REDIS_URL` with the same Redis password
- `DEEPSEEK_API_KEY`
- `OPENCODE_GO_API_KEY`
- `OLLAMA_API_KEY`
- `OLLAMA_API_BASE=https://ollama.com` for Ollama Cloud

Start it:

```bash
docker compose up -d --build
docker compose logs -f sticky-router litellm
```

## Agent Endpoint

The OpenAI-compatible endpoint for agents is:

```text
http://<nas-host>:4100/v1
```

Health check:

```bash
curl http://localhost:4100/healthz
```

Model discovery:

```bash
curl http://localhost:4100/v1/models \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

Chat smoke test:

```bash
curl http://localhost:4100/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Session-Id: smoke-test" \
  -d '{
    "messages": [{"role": "user", "content": "say OK only"}],
    "max_tokens": 80
  }'
```

The router adds these response headers:

- `X-Gateway-Model`: selected LiteLLM alias
- `X-Gateway-Reason`: `classified`, `explicit-model`, or `warm-session`

## Active Aliases

Use model aliases from `litellm.config.yaml`:

- `fast`: DeepSeek V4 Flash direct API
- `deepseek-pro`: DeepSeek V4 Pro direct API
- `opencodego-fast`: OpenCode Go `kimi-k2.7-code`
- `opencodego-code`: OpenCode Go `deepseek-v4-pro`
- `ollama-cloud`: Ollama Cloud `gemma3:27b`

Copilot Pro is intentionally skipped for now. Codex Pro is a client that can
call this gateway; it is not configured as an upstream provider.

## Routing

The router chooses aliases with deterministic rules:

- explicit allowed `model` field wins
- warm `X-Session-Id` keeps the previous model until the Redis TTL expires
- image content routes to `vision` if that alias is later enabled
- code-looking prompts route to `opencodego-fast`
- analysis/debug/design prompts route to `deepseek-pro`
- everything else routes to `fast`

Default session TTL is 600 seconds.

## Synology Container Manager

In Container Manager:

1. Create a Project.
2. Point it at this folder.
3. Use `docker-compose.yml`.
4. Create `.env` before first deploy.
5. Deploy the project.

If Container Manager creates empty folders for missing mounted files, stop the
project, create the real files, and redeploy.

## Network Exposure

Do not expose the gateway directly to the public internet.

Preferred access:

- Tailscale on the NAS, then use `http://<tailscale-ip>:4100/v1`.
- Cloudflare Tunnel with Access in front of it.
- WireGuard/VPN to your home network.

For local LAN only, use:

```text
http://<nas-lan-ip>:4100/v1
```

LiteLLM port `4000`, Postgres `5432`, and Redis `6379` are internal Docker
ports. The LiteLLM admin UI is not exposed by default.

## Updating

```bash
cd /volume1/docker/ai-gateway
docker compose pull
docker compose up -d --build
```

## Local Tests

```bash
python3 -m pip install -r router/requirements-dev.txt
PYTHONPATH=. python3 -m pytest router/tests -q
```

Back up the Docker volumes before major upgrades:

- `ai-gateway_postgres_data`
- `ai-gateway_redis_data`

## Notes

- `LITELLM_SALT_KEY` is used to encrypt stored provider credentials. Do not
  rotate it casually after creating models or keys.
- Keep `.env` only on the NAS or in a password manager.
- Put provider model changes in `litellm.config.yaml`, then redeploy.
- LiteLLM can warn that custom model costs are missing. That does not block
  calls; add `model_info` pricing later if exact spend reporting matters.

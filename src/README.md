# Synology AI Gateway

This runs a small LiteLLM gateway stack with Docker:

- LiteLLM proxy on port `4000`
- Postgres for virtual keys, spend tracking, and UI state
- Redis for cache/auth-cache state

LiteLLM's Docker docs show the proxy running with a config file and Postgres
database. The config supports model aliases, environment-loaded secrets,
`general_settings.database_url`, and Redis-backed caching.

## Files

```text
src/
  docker-compose.yml
  litellm.config.yaml
  .env.example
  README.md
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
- provider keys you actually use

Start it:

```bash
docker compose up -d
docker compose logs -f litellm
```

Test it from the NAS:

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "fast",
    "messages": [{"role": "user", "content": "say ok"}]
  }'
```

The admin UI is:

```text
http://<nas-hostname-or-ip>:4000/ui
```

Use the master key as the admin key, then create virtual keys for agents. Agents
should use virtual keys, not the master key.

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

Do not expose port `4000` directly to the public internet.

Preferred access:

- Tailscale on the NAS, then use `http://<tailscale-ip>:4000/v1`.
- Cloudflare Tunnel with Access in front of it.
- WireGuard/VPN to your home network.

For local LAN only, use:

```text
http://<nas-lan-ip>:4000/v1
```

## Agent Endpoint

The OpenAI-compatible endpoint is:

```text
http://<nas-host>:4000/v1
```

Use model aliases from `litellm.config.yaml`:

- `fast`
- `code`
- `reasoning`
- `vision`
- `openrouter`
- `local`

## Updating

```bash
cd /volume1/docker/ai-gateway
docker compose pull
docker compose up -d
```

Back up the Docker volumes before major upgrades:

- `ai-gateway_postgres_data`
- `ai-gateway_redis_data`

## Notes

- `LITELLM_SALT_KEY` is used to encrypt stored provider credentials. Do not
  rotate it casually after creating models or keys.
- Keep `.env` only on the NAS or in a password manager.
- Put provider model changes in `litellm.config.yaml`, then redeploy.
- The Coinbase-style cache-aware sticky router is a later layer. This stack is
  the shared endpoint, key management, spend tracking, fallback, and cache base.

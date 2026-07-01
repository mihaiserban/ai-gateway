# Redis usage

This page documents how the personal AI gateway uses Redis. A single Redis
instance (`redis:7-alpine`) backs two independent concerns: LiteLLM prompt
caching and router session stickiness.

## Container

Defined in `src/docker-compose.yml`:

```yaml
redis:
  image: redis:7-alpine
  container_name: ai-gateway-redis
  restart: unless-stopped
  command: redis-server --appendonly yes --requirepass "$REDIS_PASSWORD"
  environment:
    REDIS_PASSWORD: ${REDIS_PASSWORD}
  volumes:
    - redis_data:/data
  healthcheck:
    test: redis-cli -a "$REDIS_PASSWORD" ping | grep PONG
```

Redis listens on the internal Docker network only (port `6379`). It is never
published to the host. Persistence uses append-only files (`--appendonly yes`)
written to the `redis_data` named volume.

## Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `REDIS_PASSWORD` | Yes | Password for `requirepass` auth |
| `REDIS_URL` | Yes | Full connection URL, e.g. `redis://:password@redis:6379/0` |

Both are set in `.env` (see `.env.example`). `REDIS_URL` is consumed by the
router and by LiteLLM.

## Two Redis consumers

| Consumer | Keyspace | Owner | Purpose |
| --- | --- | --- | --- |
| LiteLLM prompt cache | LiteLLM-internal | LiteLLM proxy | Caches full LLM responses keyed by `prompt_cache_key` |
| Router session store | `session:*` | FastAPI router | Stores the chosen model + last-used timestamp for warm-session stickiness |

Both consumers share the same Redis instance and database (`/0`). The keyspaces
do not collide.

## LiteLLM prompt cache

LiteLLM caches complete LLM responses in Redis. When a subsequent request
arrives with the same `prompt_cache_key`, LiteLLM returns the cached response
without calling the upstream provider.

### Configuration

In `src/litellm.config.yaml` (generated from `src/gateway.config.yaml`):

```yaml
litellm_settings:
  cache: true
  cache_params:
    type: redis
    redis_url: os.environ/REDIS_URL
```

The router does not perform response caching itself. LiteLLM owns the cache
read/write logic and the internal key format.

### How the router participates

The router's role is limited to:

1. **Injecting the cache key** into the upstream request body.
2. **Reading cache-hit headers** back from LiteLLM for metrics and usage events.
3. **Forwarding** those headers to the client.

### Cache key derivation

In `src/router/main.py`:

```python
cache_key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
```

The key is a truncated SHA-256 of the `X-Session-Id` header (or a fallback
derived from the request body + auth token). It is privacy-safe: it does not
use the raw prompt content.

### Cache key gating — `cache_key_aliases`

The cache key is only sent for aliases listed in `cache_key_aliases` in
`src/gateway.config.yaml`. On each retry attempt:

```python
if current_model in app.state.route_config.cache_key_aliases:
    upstream_body["prompt_cache_key"] = cache_key
elif "prompt_cache_key" in upstream_body:
    del upstream_body["prompt_cache_key"]
```

| Scenario | Behavior |
| --- | --- |
| Model is in `cache_key_aliases` | `prompt_cache_key` is set in the upstream body |
| Model is not in `cache_key_aliases` | `prompt_cache_key` is stripped (if present) |
| Fallback from allowed to allowed alias | Same cache key is preserved across attempts |
| Fallback from allowed to non-allowed alias | Cache key is stripped on the fallback attempt |

**Default: `cache_key_aliases: []`** — prompt caching is disabled by default.

Providers that support `prompt_cache_key`: OpenAI, DeepSeek, Anthropic. Ollama
does not support it.

### Cache-hit detection

The router reads these response headers from LiteLLM:

| Header | Meaning |
| --- | --- |
| `x-litellm-cache-hit` | `true` / `false` — whether LiteLLM served from cache |
| `x-litellm-cache-key` | The cache key LiteLLM used (present when a cache lookup was attempted) |

Detection logic in `src/router/main.py`:

```python
def _cache_hit(upstream: httpx.Response) -> bool | None:
    value = upstream.headers.get("x-litellm-cache-hit")
    if value is None:
        if upstream.headers.get("x-litellm-cache-key") is not None:
            return False
        return None
    return value.lower() in {"1", "true", "yes"}
```

Three outcomes are tracked:

| Outcome | Condition |
| --- | --- |
| `hit` | `x-litellm-cache-hit: true` |
| `miss` | `x-litellm-cache-hit: false`, or cache-key header present without hit header |
| `unknown` | No cache headers at all |

Both headers are forwarded to the client (they are in the `FORWARDED_HEADERS`
allowlist).

## Router session store

The router stores the selected model and last-used timestamp per session,
enabling warm-session stickiness: a follow-up request within the TTL reuses
the same model instead of re-evaluating routing rules.

### Implementation

Defined in `src/router/sessions.py`:

| Class | Backend | When used |
| --- | --- | --- |
| `RedisSessionStore` | Redis (`session:<id>` keys with TTL) | `REDIS_URL` is set |
| `MemorySessionStore` | In-process dict with expiry | `REDIS_URL` is unset (tests, local dev) |

Both implement the `SessionStore` protocol:

```python
class SessionStore(Protocol):
    async def get(self, session_id: str) -> dict[str, Any] | None: ...
    async def set(self, session_id: str, value: dict[str, Any], ttl_seconds: int) -> None: ...
```

### Redis key format

```
session:<session_id>
```

Value is a JSON object:

```json
{
  "model": "coder",
  "last_used_ts": 1782820800.0,
  "reason": "explicit-model",
  "fallback_count": 0
}
```

TTL is set to `cache_ttl_seconds` (default 600 seconds).

### Session store selection

In `src/router/main.py`:

```python
app.state.session_store = _session_store(app.state.redis_url)

def _session_store(redis_url: str | None) -> SessionStore:
    if redis_url:
        return RedisSessionStore(redis_url)
    return MemorySessionStore()
```

### Warm-session routing

In `src/router/main.py`, warm-session stickiness only applies when the client
sends `X-Session-Id`. The stored deployment is tried first only if it still
belongs to the current resolved candidate set and is not in quota cooldown:

```python
session_key = _warm_session_key(x_session_id, auth)
session = await app.state.session_store.get(session_key)
served = session.get("served_deployment") if session else None
if isinstance(served, str) and served in resolved.ordered_deployments:
    return served
```

The session TTL is enforced by the session store with
`route_config.cache_ttl_seconds`.

### Session write

On every successful request, the router writes the session:

```python
await app.state.session_store.set(
    session_key,
    {
        "requested_model": resolved.requested_model,
        "model_kind": resolved.kind,
        "served_deployment": served_deployment,
        "timestamp": time.time(),
    },
    ttl_seconds=app.state.route_config.cache_ttl_seconds,
)
```

Failed requests do not update the session.

## Health check

The `/healthz` and `/readyz` endpoints check Redis connectivity via
`src/router/health.py`:

```python
async def check_redis(app_state: Any) -> str:
    redis_url = getattr(app_state, "redis_url", None)
    if not redis_url:
        return "disabled"
    client = redis.from_url(redis_url, decode_responses=True)
    ok = await client.ping()
    return "ok" if ok else "degraded"
```

| Status | Meaning |
| --- | --- |
| `ok` | Redis responded to `PING` |
| `degraded` | Redis unreachable or errored |
| `disabled` | `REDIS_URL` is not set (router uses `MemorySessionStore`) |

`/readyz` returns `503` if Redis status is `degraded`. A `disabled` status does
not block readiness — the router falls back to in-memory sessions.

## Metrics

Cache hit/miss/unknown counts are tracked in `src/router/metrics.py` and
exposed via the `/metrics` endpoint:

```json
{
  "cache_counts": {"hit": 0, "miss": 0, "unknown": 0}
}
```

The dashboard (`/dashboard`) displays the same counts from the
`gateway_usage_events` table, querying `cache_status` columns (`hit`, `miss`,
`unknown`).

## Configuration reference

| Setting | Default | Source | Purpose |
| --- | --- | --- | --- |
| `cache_ttl_seconds` | `600` | `gateway.config.yaml` → `router.cache_ttl_seconds` | Session TTL and warm-session window |
| `cache_key_aliases` | `[]` | `gateway.config.yaml` → `router.cache_key_aliases` | Aliases eligible for `prompt_cache_key` injection |
| `cache.type` | `redis` | `gateway.config.yaml` → `litellm.cache.type` | LiteLLM cache backend |
| `cache.redis_url_env` | `REDIS_URL` | `gateway.config.yaml` → `litellm.cache.redis_url_env` | Env var for Redis connection URL |

## Request flow

```text
Client request with X-Session-Id
  │
  ├─► SessionStore.get(session_id)
  │     └─ Redis: GET session:<id>
  │        └─ warm? reuse model (reason: warm-session)
  │
  ├─► cache_key = sha256(session_id)[:32]
  │
  ├─► if current_model in cache_key_aliases:
  │     upstream_body["prompt_cache_key"] = cache_key
  │
  └─► POST to LiteLLM
        │
        ├─ LiteLLM checks Redis (prompt cache)
        │     └─ hit → return cached response
        │     └─ miss → call upstream provider, store response
        │
        └─ response flows back
              ├─ read x-litellm-cache-hit header → metrics
              ├─ forward cache headers to client
              └─ on success: SessionStore.set(session_id, {model, ts}, ttl)
                    └─ Redis: SET session:<id> <json> EX 600
```

## Backup and restore

Redis uses append-only file persistence. See `src/README.md` → "Backup And
Restore" for the full procedure.

Backup:

```bash
docker compose exec redis redis-cli -a "$REDIS_PASSWORD" BGSAVE
docker compose cp redis:/data/appendonly.aof backups/redis-$(date +%F).aof
```

Restore:

```bash
docker cp backups/redis-YYYY-MM-DD.aof ai-gateway-redis:/data/appendonly.aof
docker compose restart redis
```

The `redis_data` named volume survives `docker compose down` (without `-v`).

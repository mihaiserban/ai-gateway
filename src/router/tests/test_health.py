import httpx
import pytest

from router.health import check_postgres
from router.main import create_app


@pytest.mark.asyncio
async def test_healthz_reports_router_ok_when_no_deps_configured():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    transport = httpx.MockTransport(handler)
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        database_url=None,
        transport=transport,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["router"] == "ok"
    assert body["redis"] == "disabled"
    assert body["postgres"] == "disabled"
    assert body["litellm"] == "degraded"
    assert body["status"] == "degraded"


@pytest.mark.asyncio
async def test_healthz_reports_router_ok_and_status_ok_when_litellm_ok():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/health/liveliness")
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        database_url=None,
        transport=transport,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["router"] == "ok"
    assert body["litellm"] == "ok"
    assert body["redis"] == "disabled"
    assert body["postgres"] == "disabled"
    assert body["status"] == "ok"


@pytest.mark.asyncio
async def test_readyz_returns_200_when_deps_ok():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        database_url=None,
        transport=transport,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"


@pytest.mark.asyncio
async def test_readyz_returns_503_when_litellm_down():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    transport = httpx.MockTransport(handler)
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        database_url=None,
        transport=transport,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not ready"
    assert body["litellm"] == "degraded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("database_url", "expected_port"),
    [
        ("postgresql://user:pass@db.internal/gateway", 5432),
        ("postgresql://user:pass@db.internal:6432/gateway", 6432),
    ],
)
async def test_check_postgres_closes_successful_connection(monkeypatch, database_url, expected_port):
    class Writer:
        closed = False
        waited = False

        def close(self):
            self.closed = True

        async def wait_closed(self):
            self.waited = True

    writer = Writer()

    async def open_connection(host, port):
        assert (host, port) == ("db.internal", expected_port)
        return object(), writer

    monkeypatch.setattr("router.health.asyncio.open_connection", open_connection)

    assert await check_postgres(database_url) == "ok"
    assert writer.closed is True
    assert writer.waited is True


@pytest.mark.asyncio
async def test_check_postgres_degrades_when_connection_fails(monkeypatch):
    async def open_connection(host, port):
        raise OSError("connection refused")

    monkeypatch.setattr("router.health.asyncio.open_connection", open_connection)

    assert await check_postgres("postgresql://db.internal/gateway") == "degraded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql:///gateway",
        "postgresql://db.internal:not-a-port/gateway",
        "postgresql://db.internal:65536/gateway",
        "postgresql://[broken/gateway",
    ],
)
async def test_check_postgres_degrades_for_malformed_url(database_url):
    assert await check_postgres(database_url) == "degraded"

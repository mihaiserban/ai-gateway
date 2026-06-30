import socket

import httpx
import pytest

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
async def test_healthz_reports_degraded_when_litellm_down():
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
    assert body["litellm"] == "degraded"
    assert body["status"] == "degraded"


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
async def test_outage_simulation():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="outage")

    transport = httpx.MockTransport(handler)
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        database_url=None,
        transport=transport,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        health = await client.get("/healthz")
        ready = await client.get("/readyz")

    assert health.status_code == 200
    assert health.json()["status"] == "degraded"
    assert health.json()["litellm"] == "degraded"

    assert ready.status_code == 503
    assert ready.json()["status"] == "not ready"


@pytest.mark.asyncio
async def test_healthz_postgres_ok_when_port_open():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    host, port = server.getsockname()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    try:
        app = create_app(
            litellm_base_url="http://litellm:4000",
            redis_url=None,
            database_url=f"postgresql://user:pass@{host}:{port}/db",
            transport=transport,
        )

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/healthz")

        body = response.json()
        assert body["postgres"] == "ok"
        assert body["status"] == "ok"
    finally:
        server.close()


@pytest.mark.asyncio
async def test_healthz_postgres_degraded_when_port_closed():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    app = create_app(
        litellm_base_url="http://litellm:4000",
        redis_url=None,
        database_url="postgresql://user:pass@127.0.0.1:1/db",
        transport=transport,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/healthz")

    body = response.json()
    assert body["postgres"] == "degraded"
    assert body["status"] == "degraded"
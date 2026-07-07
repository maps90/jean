from __future__ import annotations

from aiohttp.test_utils import TestClient, TestServer

from jean.health import make_health_app


async def test_healthz_returns_200():
    app = make_health_app(ready_check=lambda: _ready(True))
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        assert resp.status == 200


async def test_readyz_returns_200_when_ready():
    app = make_health_app(ready_check=lambda: _ready(True))
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/readyz")
        assert resp.status == 200


async def test_readyz_returns_503_when_not_ready():
    app = make_health_app(ready_check=lambda: _ready(False))
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/readyz")
        assert resp.status == 503


async def _ready(value: bool) -> bool:
    return value

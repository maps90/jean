from __future__ import annotations

import logging

from aiohttp.test_utils import TestClient, TestServer

from jean import __version__
from jean.health import ErrorOnlyAccessLogger, make_health_app


async def test_healthz_returns_200():
    app = make_health_app(ready_check=lambda: _ready(True))
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        assert resp.status == 200


async def test_healthz_reports_running_version():
    app = make_health_app(ready_check=lambda: _ready(True))
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        body = await resp.json()
        assert body["version"] == __version__


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


class _Req:
    method = "GET"
    path = "/healthz"


class _Resp:
    def __init__(self, status: int) -> None:
        self.status = status


def test_access_log_is_silent_on_success(caplog):
    """Kubernetes probes /healthz and /readyz every few seconds; aiohttp's
    default access logger turns that into a 200 every tick and buries every
    real line. Successful probes must say nothing."""
    logger = logging.getLogger("jean.health.access")
    access = ErrorOnlyAccessLogger(logger, log_format="")

    with caplog.at_level(logging.DEBUG, logger="jean.health.access"):
        access.log(_Req(), _Resp(200), 0.001)

    assert caplog.records == []


def test_access_log_reports_failures(caplog):
    """A failing probe (readyz 503 when Postgres is unreachable, or any 4xx/5xx)
    is the one thing worth hearing about."""
    logger = logging.getLogger("jean.health.access")
    access = ErrorOnlyAccessLogger(logger, log_format="")

    with caplog.at_level(logging.DEBUG, logger="jean.health.access"):
        access.log(_Req(), _Resp(503), 0.002)
        access.log(_Req(), _Resp(404), 0.001)

    assert len(caplog.records) == 2
    assert "503" in caplog.text
    assert "/healthz" in caplog.text

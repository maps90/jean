from __future__ import annotations

from collections.abc import Awaitable, Callable

from aiohttp import web
from aiohttp.abc import AbstractAccessLogger

from jean import __version__


class ErrorOnlyAccessLogger(AbstractAccessLogger):
    """Access logger that says nothing about a successful request.

    The liveness/readiness probes poll /healthz and /readyz every few seconds
    forever, and aiohttp's default access logger emits a line per request -- so
    in a deployed pod the health traffic drowns out every log line that
    actually matters. Only failures are worth hearing about: a 503 from /readyz
    (Postgres unreachable) or any other 4xx/5xx.
    """

    def log(self, request: object, response: object, time: float) -> None:
        status = getattr(response, "status", 0)
        if status >= 400:
            self.logger.warning(
                "%s %s -> %d (%.3fs)",
                getattr(request, "method", "?"),
                getattr(request, "path", "?"),
                status,
                time,
            )


def make_health_app(*, ready_check: Callable[[], Awaitable[bool]]) -> web.Application:
    """Liveness/readiness endpoints for the health-port server. `/healthz`
    always answers if the process is up and reports the running `version` so
    operators can confirm which build a replica is serving; `/readyz` runs
    `ready_check` (in server.py, `store.ping`) so orchestrators can hold traffic
    until Postgres is reachable."""

    async def healthz(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "version": __version__})

    async def readyz(_request: web.Request) -> web.Response:
        ready = await ready_check()
        return web.json_response({"ready": ready}, status=200 if ready else 503)

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)
    return app

from __future__ import annotations

from collections.abc import Awaitable, Callable

from aiohttp import web


def make_health_app(*, ready_check: Callable[[], Awaitable[bool]]) -> web.Application:
    """Liveness/readiness endpoints for the health-port server. `/healthz`
    always answers if the process is up; `/readyz` runs `ready_check` (in
    server.py, `store.ping`) so orchestrators can hold traffic until Postgres
    is reachable."""

    async def healthz(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def readyz(_request: web.Request) -> web.Response:
        ready = await ready_check()
        return web.json_response({"ready": ready}, status=200 if ready else 503)

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)
    return app

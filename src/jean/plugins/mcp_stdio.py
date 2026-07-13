from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from jean.plugins.env_refs import expand

# Spawning and reaping a stdio MCP server. jean runs these itself (see
# mcp_client.py) rather than letting the CLI fork its own copy per session --
# stdio transport *is* a child process, so per-session clients cannot share one.

PROTOCOL_VERSION = "2024-11-05"

# One JSON-RPC frame is one line, and a `tools/list` reply carries every tool's
# full input schema: mcp-grafana's runs to well over asyncio's default 64 KiB
# stream limit, at which readline() raises instead of returning the line. Left at
# the default, a healthy server was reported as down -- 16 MiB is far past any
# plausible tool list, and is only ever a buffer ceiling, not an allocation.
STREAM_LIMIT = 16 * 1024 * 1024


class McpProcess(Protocol):
    """Structural: `asyncio.subprocess.Process` satisfies this as-is."""

    stdin: Any
    stdout: Any
    stderr: Any

    def terminate(self) -> None: ...
    async def wait(self) -> int: ...


SpawnFn = Callable[[dict[str, Any]], Awaitable[McpProcess]]


async def spawn_stdio_server(config: dict[str, Any]) -> McpProcess:
    env = {**os.environ, **{k: expand(str(v)) for k, v in (config.get("env") or {}).items()}}
    return await asyncio.create_subprocess_exec(
        config["command"],
        *config.get("args", []),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        limit=STREAM_LIMIT,
    )


async def stderr_tail(proc: McpProcess, *, max_lines: int = 8, max_chars: int = 400) -> str:
    """Why the server died, as it actually prints it.

    Not just the last line: npx trails npm warnings, and a server that rejects
    its config exits with a multi-line JSON blob ending in a bare `]`. The tail,
    joined and bounded, keeps the line that names the cause.
    """
    try:
        data = await asyncio.wait_for(proc.stderr.read(), 2)
    except (TimeoutError, OSError):
        data = b""
    lines = [ln.strip() for ln in data.decode(errors="replace").splitlines() if ln.strip()]
    if not lines:
        return "server exited without a reply"
    tail = " | ".join(lines[-max_lines:])
    return tail if len(tail) <= max_chars else "…" + tail[-max_chars:]


async def reap(proc: McpProcess) -> None:
    # Best-effort: the child may already be gone (that is often the failure being
    # handled), and jean must never leave a stray process behind either way.
    try:
        proc.stdin.close()
        proc.terminate()
    except (ProcessLookupError, OSError):
        return
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), 5)

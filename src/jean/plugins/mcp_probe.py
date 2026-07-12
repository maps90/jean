from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# The MCP servers a plugin declares are spawned by the *CLI child*, not by jean,
# and the CLI never tells the SDK whether they came up: its init message reports
# every plugin server as "pending" and carries no tools for them. A server that
# dies at startup (an `npx` command that resolves to nothing, say) therefore
# leaves the thread silently tool-less for its entire life -- the CLI does not
# retry inside a session. So jean speaks MCP itself at boot: spawn each server,
# do the real `initialize` + `tools/list` handshake, log the outcome, and retry
# the ones that failed before any Slack thread exists.

PROTOCOL_VERSION = "2024-11-05"

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class ProbeResult:
    server: str
    connected: bool
    tool_count: int = 0
    error: str | None = None
    attempts: int = 1


class McpProcess(Protocol):
    """Structural: `asyncio.subprocess.Process` satisfies this as-is."""

    stdin: Any
    stdout: Any
    stderr: Any

    def terminate(self) -> None: ...
    async def wait(self) -> int: ...


SpawnFn = Callable[[dict[str, Any]], Awaitable[McpProcess]]


def _expand(value: str) -> str:
    """`${ES_URL}` in a plugin's .mcp.json -- the CLI expands these, so must we."""
    return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)


async def spawn_stdio_server(config: dict[str, Any]) -> McpProcess:
    env = {**os.environ, **{k: _expand(str(v)) for k, v in (config.get("env") or {}).items()}}
    return await asyncio.create_subprocess_exec(
        config["command"],
        *config.get("args", []),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )


async def _request(proc: McpProcess, message: dict[str, Any]) -> dict[str, Any]:
    proc.stdin.write(json.dumps(message).encode() + b"\n")
    await proc.stdin.drain()
    while True:
        line = await proc.stdout.readline()
        if not line:
            raise EOFError("server closed its output")
        try:
            reply = json.loads(line)
        except json.JSONDecodeError:
            continue  # servers are allowed to print noise before their first frame
        if reply.get("id") != message["id"]:
            continue
        if "error" in reply:
            raise RuntimeError(str(reply["error"]))
        return reply.get("result", {})


async def _handshake(proc: McpProcess) -> int:
    await _request(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "jean-preflight", "version": "1"},
            },
        },
    )
    notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    proc.stdin.write(json.dumps(notification).encode() + b"\n")
    await proc.stdin.drain()
    result = await _request(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    return len(result.get("tools", []))


async def _stderr_tail(proc: McpProcess, *, max_lines: int = 8, max_chars: int = 400) -> str:
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


async def _reap(proc: McpProcess) -> None:
    # Best-effort: the child may already be gone (that is the failure we probe
    # for), and a boot check must never leave a stray process behind either way.
    try:
        proc.stdin.close()
        proc.terminate()
    except (ProcessLookupError, OSError):
        return
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), 5)


async def probe_server(
    name: str,
    config: dict[str, Any],
    *,
    spawn: SpawnFn = spawn_stdio_server,
    timeout: float = 60.0,
    attempt: int = 1,
) -> ProbeResult:
    """Spawn one MCP server and complete a real handshake with it."""
    proc: McpProcess | None = None
    try:
        proc = await spawn(config)
        tool_count = await asyncio.wait_for(_handshake(proc), timeout)
        return ProbeResult(name, connected=True, tool_count=tool_count, attempts=attempt)
    except TimeoutError:
        return ProbeResult(
            name, connected=False, error=f"timed out after {timeout:.0f}s", attempts=attempt
        )
    except EOFError:
        error = await _stderr_tail(proc) if proc else "server never started"
        return ProbeResult(name, connected=False, error=error, attempts=attempt)
    except (OSError, RuntimeError, ValueError) as exc:
        return ProbeResult(
            name, connected=False, error=f"{type(exc).__name__}: {exc}", attempts=attempt
        )
    finally:
        if proc is not None:
            await _reap(proc)


async def preflight(
    servers: dict[str, dict[str, Any]],
    *,
    spawn: SpawnFn = spawn_stdio_server,
    attempts: int = 3,
    delay: float = 2.0,
    timeout: float = 60.0,
) -> list[ProbeResult]:
    """Load every MCP server once at boot, retrying the ones that fail.

    Servers are probed concurrently, as the CLI spawns them: the timeout is
    generous because a first `npx` pays for the package download (measured at
    over a minute for a cold one), and serialising would stack those waits in
    front of Slack. A server that stays down is logged and skipped -- jean must
    still come up for the servers that do work.
    """
    if not servers:
        return []
    logger.info("probing %d MCP server(s)", len(servers))
    return list(
        await asyncio.gather(
            *(
                _probe_with_retry(
                    name, config, spawn=spawn, attempts=attempts, delay=delay, timeout=timeout
                )
                for name, config in servers.items()
            )
        )
    )


async def _probe_with_retry(
    name: str,
    config: dict[str, Any],
    *,
    spawn: SpawnFn,
    attempts: int,
    delay: float,
    timeout: float,
) -> ProbeResult:
    result = ProbeResult(name, connected=False, error="not probed")
    for attempt in range(1, attempts + 1):
        result = await probe_server(name, config, spawn=spawn, timeout=timeout, attempt=attempt)
        if result.connected:
            logger.info(
                "mcp %s: connected (%d tools, attempt %d/%d)",
                name,
                result.tool_count,
                attempt,
                attempts,
            )
            return result
        if attempt < attempts:
            logger.warning(
                "mcp %s: attempt %d/%d failed: %s -- retrying in %.0fs",
                name,
                attempt,
                attempts,
                result.error,
                delay,
            )
            await asyncio.sleep(delay)
    logger.warning(
        "mcp %s: gave up after %d attempt(s): %s -- its tools will be unavailable",
        name,
        attempts,
        result.error,
    )
    return result

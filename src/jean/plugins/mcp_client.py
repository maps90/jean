from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from jean.plugins.mcp_stdio import (
    PROTOCOL_VERSION,
    McpProcess,
    SpawnFn,
    reap,
    spawn_stdio_server,
    stderr_tail,
)

logger = logging.getLogger(__name__)

# A cold `npx` pays for the package download before the server says a word
# (measured at over a minute in the pod), so the handshake gets a generous window.
DEFAULT_START_TIMEOUT = 60.0

# An upstream tool call is a human-timescale operation (a kubectl list, a
# Grafana query), not an instant one -- but it must not outlive the patience of
# the Slack thread waiting on it.
DEFAULT_CALL_TIMEOUT = 120.0


class _Disconnected(EOFError):
    """The upstream process is gone. Distinct from a tool error the server
    *returned*: this one is safe to retry, that one is an answer."""


class McpClient:
    """One long-lived stdio MCP server, shared by every session in this worker.

    stdio transport *is* a child process, so the CLI cannot share one: each
    ClaudeSDKClient it spawns forks its own copy of every configured server. At
    ~250 MB per set that is what pinned the pod against its memory limit with
    two threads open. So jean runs each server once, itself, and re-exposes the
    tools in-process (see mcp_proxy.py) -- the CLI then spawns nothing.

    Being shared makes concurrency real: two Slack threads can have calls in
    flight down this one pipe at the same time. Replies are therefore matched by
    JSON-RPC id (`_pending`), never by arrival order, and writes are serialized
    so two frames cannot interleave on stdin.
    """

    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        *,
        spawn: SpawnFn = spawn_stdio_server,
        call_timeout: float = DEFAULT_CALL_TIMEOUT,
    ) -> None:
        self._name = name
        self._config = config
        self._spawn = spawn
        self._call_timeout = call_timeout
        self._proc: McpProcess | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._restart_lock = asyncio.Lock()
        self._tools: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def tools(self) -> list[dict[str, Any]]:
        return self._tools

    async def start(self) -> list[dict[str, Any]]:
        """Spawn the server, complete the MCP handshake, and cache its tools."""
        self._proc = await self._spawn(self._config)
        self._reader = asyncio.create_task(self._read_loop(self._proc))
        await self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "jean", "version": "1"},
            },
        )
        await self._notify("notifications/initialized")
        result = await self._request("tools/list", {})
        self._tools = result.get("tools", [])
        return self._tools

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke an upstream tool, in the shape the SDK wants back.

        Every failure is returned as an `is_error` result rather than raised:
        this runs inside the agent's tool loop, where a raised exception kills
        the whole turn, but an error result is something the agent can read,
        report in the thread, and work around.
        """
        try:
            try:
                result = await self._attempt(tool, arguments)
            except _Disconnected:
                # The upstream died -- possibly long ago, possibly mid-call. Only
                # a *transport* failure is retried: a tool error the server chose
                # to return is an answer, and re-running the call could repeat
                # whatever it already did.
                await self._restart()
                result = await self._attempt(tool, arguments)
        except TimeoutError:
            return _error(f"{self._name}: {tool} timed out after {self._call_timeout:.0f}s")
        except (_Disconnected, OSError, RuntimeError, ValueError) as exc:
            return _error(f"{self._name}: {tool} failed: {type(exc).__name__}: {exc}")
        # An upstream's own tool error (isError) is a legitimate answer -- pass it
        # through as one, renamed to the SDK's spelling.
        content = result.get("content", [])
        if result.get("isError"):
            return {"content": content, "is_error": True}
        return {"content": content}

    async def _attempt(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self._alive():
            await self._restart()
        return await asyncio.wait_for(
            self._request("tools/call", {"name": tool, "arguments": arguments}),
            self._call_timeout,
        )

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        if self._proc is not None:
            await reap(self._proc)
            self._proc = None

    def _alive(self) -> bool:
        return self._proc is not None and self._reader is not None and not self._reader.done()

    async def _restart(self) -> None:
        async with self._restart_lock:
            if self._alive():  # another caller won the race and already rebuilt it
                return
            reason = await stderr_tail(self._proc) if self._proc is not None else "never started"
            logger.warning("mcp %s: upstream is gone (%s) -- restarting", self._name, reason)
            await self.close()
            await self.start()

    async def _read_loop(self, proc: McpProcess) -> None:
        """Dispatch replies to their waiting caller until the child goes away."""
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break  # EOF: the child exited
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    continue  # servers may print noise before their first frame
                future = self._pending.pop(frame.get("id"), None)
                if future is None or future.done():
                    continue  # a notification, or a reply to a call that gave up
                if "error" in frame:
                    future.set_exception(RuntimeError(str(frame["error"])))
                else:
                    future.set_result(frame.get("result", {}))
        finally:
            # Nobody is left to answer these. Failing them now turns a silent
            # wait-out-the-timeout into an immediate, retryable disconnect --
            # which is also what a caller that raced the EOF (wrote into a pipe
            # whose reader had not yet noticed it was dead) is waiting on.
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(_Disconnected(f"{self._name}: server closed its output"))
            self._pending.clear()

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            # Register *then* check, in that order. The reader is what fails
            # pending futures on EOF, so a request that registers after the
            # reader has already exited would have nobody left to answer it and
            # would wait out the full call_timeout. Checking after registering
            # leaves no gap: either the reader is still up (and will fail this
            # future when the child dies), or it is already gone and we say so
            # now -- which the caller retries by restarting the child.
            if not self._alive():
                raise _Disconnected(f"{self._name}: server is not running")
            await self._write(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def _notify(self, method: str) -> None:
        await self._write({"jsonrpc": "2.0", "method": method})

    async def _write(self, frame: dict[str, Any]) -> None:
        if self._proc is None:  # pragma: no cover -- start() always sets it first
            raise RuntimeError(f"{self._name}: not started")
        async with self._write_lock:
            try:
                self._proc.stdin.write(json.dumps(frame).encode() + b"\n")
                await self._proc.stdin.drain()
            except OSError as exc:  # BrokenPipe/ConnectionReset: the child is gone
                raise _Disconnected(f"{self._name}: {exc}") from exc


def _error(text: str) -> dict[str, Any]:
    logger.warning("mcp call failed: %s", text)
    return {"content": [{"type": "text", "text": text}], "is_error": True}


async def start_clients(
    servers: dict[str, dict[str, Any]],
    *,
    spawn: SpawnFn = spawn_stdio_server,
    attempts: int = 3,
    delay: float = 2.0,
    timeout: float = DEFAULT_START_TIMEOUT,
    call_timeout: float = DEFAULT_CALL_TIMEOUT,
) -> list[McpClient]:
    """Bring every MCP server up once, at boot, and keep the ones that answer.

    This is also jean's preflight: the CLI never reports whether a server it
    spawned came up (its init message calls them all "pending" and carries none
    of their tools), so a broken server used to leave a thread silently
    tool-less for its whole life. Connecting here means a failure is visible in
    the log before any Slack thread exists -- and now the connection that proved
    the server works is the very one every thread will use.

    Servers start concurrently: a cold `npx` pays for its download, and doing
    that serially would stack those waits in front of Slack. One that stays down
    is logged and skipped; jean must still come up for the servers that work.
    """
    if not servers:
        return []
    logger.info("starting %d MCP server(s)", len(servers))
    started = await asyncio.gather(
        *(
            _start_with_retry(
                name,
                config,
                spawn=spawn,
                attempts=attempts,
                delay=delay,
                timeout=timeout,
                call_timeout=call_timeout,
            )
            for name, config in servers.items()
        )
    )
    clients = [c for c in started if c is not None]
    failed = [name for name, client in zip(servers, started, strict=True) if client is None]
    _log_summary(clients, total=len(servers), failed=failed)
    return clients


async def _start_with_retry(
    name: str,
    config: dict[str, Any],
    *,
    spawn: SpawnFn,
    attempts: int,
    delay: float,
    timeout: float,
    call_timeout: float,
) -> McpClient | None:
    for attempt in range(1, attempts + 1):
        client = McpClient(name, config, spawn=spawn, call_timeout=call_timeout)
        try:
            tools = await asyncio.wait_for(client.start(), timeout)
        except (FileNotFoundError, PermissionError) as exc:
            # The command itself is missing or unrunnable (the grafana plugin
            # asks for `docker`, which the image has not got). No retry will
            # conjure it, so do not spend the delay finding that out twice.
            await client.close()
            logger.warning(
                "mcp %s: cannot run %r: %s -- its tools will be unavailable",
                name,
                config.get("command"),
                exc.strerror or exc,
            )
            return None
        except (TimeoutError, OSError, EOFError, RuntimeError, ValueError) as exc:
            reason = (
                f"timed out after {timeout:.0f}s" if isinstance(exc, TimeoutError) else str(exc)
            )
            await client.close()
            if attempt < attempts:
                logger.warning(
                    "mcp %s: attempt %d/%d failed: %s -- retrying in %.0fs",
                    name,
                    attempt,
                    attempts,
                    reason,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            logger.warning(
                "mcp %s: gave up after %d attempt(s): %s -- its tools will be unavailable",
                name,
                attempts,
                reason,
            )
            return None
        logger.info(
            "mcp %s: connected (%d tools, attempt %d/%d)", name, len(tools), attempt, attempts
        )
        return client
    return None  # pragma: no cover -- the loop always returns


def _log_summary(clients: list[McpClient], *, total: int, failed: list[str]) -> None:
    """The one line an operator reads: the score, and both sides of it by name.

    Without it the outcome is scattered across per-server warnings, and "which
    tools does jean actually have right now" becomes a reassembly job.
    """
    connected = ", ".join(f"{c.name} ({len(c.tools)} tools)" for c in clients) or "none"
    summary = f"{len(clients)}/{total} MCP servers connected: {connected}"
    if failed:
        summary += " -- failed: " + ", ".join(failed)
    (logger.warning if failed else logger.info)(summary)

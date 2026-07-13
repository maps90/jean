from __future__ import annotations

import asyncio
import json
import logging
import sys

from jean.plugins.mcp_client import McpClient, start_clients


class _Stdin:
    def __init__(self, server: FakeServer) -> None:
        self._server = server

    def write(self, data: bytes) -> None:
        self._server.handle(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _Stdout:
    def __init__(self, queue: asyncio.Queue[bytes]) -> None:
        self._queue = queue

    async def readline(self) -> bytes:
        return await self._queue.get()


class _Stderr:
    async def read(self) -> bytes:
        return b"upstream died"


class FakeServer:
    """A long-lived stdio MCP server, faked at the pipe level.

    Unlike the probe's fake this answers tools/call, and can hold a call open
    (`defer`) so a test can prove replies are matched by id rather than by
    arrival order.
    """

    def __init__(
        self,
        *,
        tools: list[str],
        defer: set[str] = frozenset(),
        flush_after: int | None = None,
    ):
        self._tools = tools
        self._defer = defer
        self._flush_after = flush_after
        self._deferred: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.alive = True
        self.terminated = False
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.stdin = _Stdin(self)
        self.stdout = _Stdout(self._queue)
        self.stderr = _Stderr()

    def _emit(self, msg_id, result=None, error=None) -> None:
        frame = {"jsonrpc": "2.0", "id": msg_id}
        frame["error" if error else "result"] = error or result
        self._queue.put_nowait(json.dumps(frame).encode() + b"\n")

    def handle(self, data: bytes) -> None:
        if not self.alive:
            return
        msg = json.loads(data)
        method = msg.get("method")
        if method == "initialize":
            self._emit(msg["id"], {"protocolVersion": "2024-11-05"})
        elif method == "tools/list":
            self._emit(
                msg["id"],
                {
                    "tools": [
                        {
                            "name": t,
                            "description": f"{t} does a thing",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"ns": {"type": "string"}},
                            },
                        }
                        for t in self._tools
                    ]
                },
            )
        elif method == "tools/call":
            name = msg["params"]["name"]
            args = msg["params"].get("arguments", {})
            self.calls.append((name, args))
            payload = {"content": [{"type": "text", "text": f"{name} ran with {args}"}]}
            if name in self._defer:
                self._deferred.append({"id": msg["id"], "result": payload})
                if self._flush_after is not None and len(self.calls) >= self._flush_after:
                    self.flush_deferred()
                return
            self._emit(msg["id"], payload)

    def flush_deferred(self) -> None:
        """Answer the held calls, newest first -- replies out of request order."""
        for frame in reversed(self._deferred):
            self._emit(frame["id"], frame["result"])
        self._deferred.clear()

    def die(self) -> None:
        self.alive = False
        self._queue.put_nowait(b"")  # EOF

    def terminate(self) -> None:
        self.terminated = True

    async def wait(self) -> int:
        return 0


def _client(servers: list[FakeServer], **kwargs) -> McpClient:
    spawned = iter(servers)

    async def spawn(config):
        return next(spawned)

    # A short call_timeout by default: a bug that wedges a call should fail the
    # test in a moment, not sit on the real 120s production timeout.
    kwargs.setdefault("call_timeout", 5.0)
    return McpClient("kubernetes", {"command": "npx"}, spawn=spawn, **kwargs)


async def test_start_returns_the_upstream_tools_with_their_schemas():
    server = FakeServer(tools=["pods_list", "pods_log"])
    client = _client([server])

    tools = await client.start()

    assert [t["name"] for t in tools] == ["pods_list", "pods_log"]
    assert tools[0]["inputSchema"]["properties"]["ns"]["type"] == "string"


async def test_call_forwards_arguments_and_returns_the_content():
    server = FakeServer(tools=["pods_list"])
    client = _client([server])
    await client.start()

    result = await client.call("pods_list", {"ns": "devops"})

    assert server.calls == [("pods_list", {"ns": "devops"})]
    assert "pods_list ran with" in result["content"][0]["text"]
    assert not result.get("is_error")


async def test_concurrent_calls_are_matched_by_id_not_by_arrival_order():
    """Every Slack thread now shares one upstream process, so two threads can
    have calls in flight at once. Replies come back interleaved -- pairing them
    by arrival order would hand thread A the answer to thread B's question."""
    # Both calls are held until both have arrived, then answered in reverse --
    # so `fast` replies before `slow`, out of request order.
    server = FakeServer(tools=["slow", "fast"], defer={"slow", "fast"}, flush_after=2)
    client = _client([server])
    await client.start()

    first = asyncio.create_task(client.call("slow", {"ns": "a"}))
    second = asyncio.create_task(client.call("fast", {"ns": "b"}))

    assert "slow ran with {'ns': 'a'}" in (await first)["content"][0]["text"]
    assert "fast ran with {'ns': 'b'}" in (await second)["content"][0]["text"]


async def test_a_dead_upstream_is_restarted_on_the_next_call():
    """One process per pod now serves every thread for the pod's whole life, so
    a crash must not leave jean permanently tool-less until the next deploy."""
    first, second = FakeServer(tools=["pods_list"]), FakeServer(tools=["pods_list"])
    client = _client([first, second])
    await client.start()
    first.die()

    result = await client.call("pods_list", {"ns": "devops"})

    assert second.calls == [("pods_list", {"ns": "devops"})]
    assert "pods_list ran with" in result["content"][0]["text"]


async def test_a_call_that_outlives_its_timeout_is_an_error_not_a_hang():
    """A wedged upstream must not hold the turn open forever."""
    server = FakeServer(tools=["slow"], defer={"slow"})
    client = _client([server], call_timeout=0.05)
    await client.start()

    result = await client.call("slow", {})

    assert result["is_error"] is True
    assert "timed out" in result["content"][0]["text"]


async def test_close_terminates_the_child():
    server = FakeServer(tools=["pods_list"])
    client = _client([server])
    await client.start()

    await client.close()

    assert server.terminated is True


# --- start_clients: jean's boot-time load, which is also its preflight ---


_BIG_SERVER = """
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        result = {"protocolVersion": "2024-11-05", "serverInfo": {"name": "big"}}
    elif msg.get("method") == "tools/list":
        result = {"tools": [
            {"name": f"tool_{i}", "description": "x" * 900} for i in range(200)
        ]}
    else:
        continue
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\\n")
    sys.stdout.flush()
"""


class DeadServer(FakeServer):
    """A server that exits at startup, as a cold `npx` does when it resolves to
    nothing: it prints to stderr and closes its output."""

    def __init__(self, stderr: str) -> None:
        super().__init__(tools=[])
        self._stderr = stderr
        self.alive = False
        self.stderr = _DeadStderr(stderr.encode())
        self._queue.put_nowait(b"")  # EOF


class _DeadStderr:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self) -> bytes:
        return self._data


async def test_a_server_with_a_large_tool_list_connects(tmp_path):
    """Seen in production against mcp-grafana: its tools/list reply is one line
    of well over 64 KiB, and asyncio's default stream limit made readline() raise
    "Separator is found, but chunk is longer than limit" -- so a healthy server
    looked dead. Uses a real subprocess, since the limit lives in the transport."""
    clients = await start_clients(
        {"big": {"command": sys.executable, "args": ["-c", _BIG_SERVER]}}, timeout=30
    )

    assert len(clients) == 1
    assert len(clients[0].tools) == 200
    await clients[0].close()


async def test_a_transient_failure_is_retried_and_recovers():
    """The outage this exists for: the first npx spawn of a fresh pod failed
    ("not found") while a later, identical one succeeded."""
    spawns = []

    async def spawn(config):
        spawns.append(config)
        if len(spawns) == 1:
            return DeadServer("sh: 1: kubernetes-mcp-server: not found\n")
        return FakeServer(tools=["pods_list"])

    clients = await start_clients(
        {"kubernetes": {"command": "npx"}}, spawn=spawn, attempts=3, delay=0
    )

    assert [c.name for c in clients] == ["kubernetes"]
    assert len(spawns) == 2, "it must stop retrying once the server is up"


async def test_a_missing_command_is_not_retried():
    """Seen in production: the grafana plugin runs its server via `docker`, which
    the image does not have. A command that is absent will not appear on the
    second attempt -- retrying it only stalls the boot it runs in front of."""
    spawns = []

    async def spawn(config):
        spawns.append(config)
        raise FileNotFoundError(2, "No such file or directory", "docker")

    clients = await start_clients(
        {"grafana": {"command": "docker"}}, spawn=spawn, attempts=3, delay=0
    )

    assert clients == []
    assert len(spawns) == 1, "a missing binary must not be retried"


async def test_a_broken_server_does_not_stop_the_working_ones_from_loading(caplog):
    async def spawn(config):
        if config["command"] == "broken":
            return DeadServer("sh: 1: mcp-server-elasticsearch: not found\n")
        return FakeServer(tools=["pods_list", "pods_log"])

    with caplog.at_level(logging.INFO, logger="jean.plugins.mcp_client"):
        clients = await start_clients(
            {"kubernetes": {"command": "npx"}, "elasticsearch": {"command": "broken"}},
            spawn=spawn,
            attempts=1,
            delay=0,
        )

    assert [c.name for c in clients] == ["kubernetes"]
    assert "1/2 MCP servers connected: kubernetes (2 tools)" in caplog.text
    assert "failed: elasticsearch" in caplog.text

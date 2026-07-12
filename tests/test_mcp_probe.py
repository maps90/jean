from __future__ import annotations

import asyncio
import json
import logging

from jean.plugins.mcp_probe import preflight, probe_server


class _Stdin:
    def __init__(self, server: FakeProc) -> None:
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
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self) -> bytes:
        return self._data


class FakeProc:
    """A stdio MCP server, faked at the pipe level.

    `stderr=...` models a server that dies at startup (npx printing
    "command not found" and exiting); `hang=True` models one that never speaks.
    """

    def __init__(self, *, tools: list[str] | None = None, stderr: str = "", hang: bool = False):
        self._tools = tools or []
        self._dead = bool(stderr)
        self._hang = hang
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.stdin = _Stdin(self)
        self.stdout = _Stdout(self._queue)
        self.stderr = _Stderr(stderr.encode())
        self.terminated = False
        if self._dead:
            self._queue.put_nowait(b"")  # EOF: the child is gone

    def handle(self, data: bytes) -> None:
        if self._dead or self._hang:
            return
        msg = json.loads(data)
        if msg.get("method") == "initialize":
            result = {"protocolVersion": "2024-11-05", "serverInfo": {"name": "fake"}}
        elif msg.get("method") == "tools/list":
            result = {"tools": [{"name": t} for t in self._tools]}
        else:
            return  # notifications carry no id and expect no reply
        line = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}).encode()
        self._queue.put_nowait(line + b"\n")

    def terminate(self) -> None:
        self.terminated = True

    async def wait(self) -> int:
        return 0


async def test_a_healthy_server_reports_the_tools_it_serves():
    async def spawn(config):
        return FakeProc(tools=["pods_list", "pods_log"])

    result = await probe_server("kubernetes", {"command": "npx"}, spawn=spawn)

    assert result.connected
    assert result.tool_count == 2
    assert result.attempts == 1


async def test_a_dead_server_surfaces_the_reason_it_died():
    async def spawn(config):
        return FakeProc(stderr="sh: 1: kubernetes-mcp-server: not found\n")

    result = await probe_server("kubernetes", {"command": "npx"}, spawn=spawn)

    assert not result.connected
    assert result.tool_count == 0
    assert "kubernetes-mcp-server: not found" in result.error


async def test_the_reason_survives_the_noise_printed_after_it():
    """Real stderr does not end on the useful line: npx trails npm warnings, and
    the elasticsearch server exits with a multi-line JSON blob whose last line is
    a bare `]`. Reporting only the final line reported `]` -- useless."""

    async def spawn(config):
        return FakeProc(
            stderr=(
                "npm WARN deprecated glob@10.5.0: old versions are not supported\n"
                '  "message": "Invalid Elasticsearch URL format",\n'
                '  "path": [\n    "url"\n  ]\n]\n'
            )
        )

    result = await probe_server("elasticsearch", {"command": "npx"}, spawn=spawn)

    assert not result.connected
    assert "Invalid Elasticsearch URL format" in result.error


async def test_a_silent_server_times_out_rather_than_hanging_boot():
    proc = FakeProc(hang=True)

    async def spawn(config):
        return proc

    result = await probe_server("kubernetes", {"command": "npx"}, spawn=spawn, timeout=0.05)

    assert not result.connected
    assert "timed out" in result.error
    assert proc.terminated, "a hung child must not be left running"


async def test_the_child_is_always_reaped():
    proc = FakeProc(tools=["pods_list"])

    async def spawn(config):
        return proc

    await probe_server("kubernetes", {"command": "npx"}, spawn=spawn)

    assert proc.terminated


async def test_preflight_retries_a_failed_load_and_recovers():
    """The outage this exists for: the first npx spawn of a fresh pod failed
    ("not found") while a later, identical one succeeded."""
    attempts = []

    async def spawn(config):
        attempts.append(config)
        if len(attempts) == 1:
            return FakeProc(stderr="sh: 1: kubernetes-mcp-server: not found\n")
        return FakeProc(tools=["pods_list"])

    results = await preflight({"kubernetes": {"command": "npx"}}, spawn=spawn, attempts=3, delay=0)

    assert [r.connected for r in results] == [True]
    assert results[0].attempts == 2
    assert len(attempts) == 2, "it must stop retrying once the server is up"


async def test_a_missing_command_is_not_retried():
    """Seen in production: the grafana plugin runs its server via `docker`, which
    the image does not have. A command that is absent will not appear on the
    second attempt -- retrying it only stalls the boot it runs in front of."""
    spawns = []

    async def spawn(config):
        spawns.append(config)
        raise FileNotFoundError(2, "No such file or directory", "docker")

    results = await preflight({"grafana": {"command": "docker"}}, spawn=spawn, attempts=3, delay=0)

    assert not results[0].connected
    assert results[0].attempts == 1
    assert "docker" in results[0].error
    assert len(spawns) == 1, "a missing binary must not be retried"


async def test_a_transient_failure_is_still_retried():
    """The distinction that matters: the server started and then died, which is
    what a cold npx does -- that one recovers on a second attempt."""
    spawns = []

    async def spawn(config):
        spawns.append(config)
        if len(spawns) == 1:
            return FakeProc(stderr="sh: 1: kubernetes-mcp-server: not found\n")
        return FakeProc(tools=["pods_list"])

    results = await preflight({"kubernetes": {"command": "npx"}}, spawn=spawn, attempts=3, delay=0)

    assert results[0].connected
    assert len(spawns) == 2


async def test_preflight_gives_up_on_a_permanently_broken_server_without_killing_boot():
    async def spawn(config):
        return FakeProc(stderr="sh: 1: mcp-server-elasticsearch: not found\n")

    results = await preflight(
        {"elasticsearch": {"command": "npx"}, "kubernetes": {"command": "npx"}},
        spawn=spawn,
        attempts=2,
        delay=0,
    )

    assert [r.connected for r in results] == [False, False]
    assert all(r.attempts == 2 for r in results)


async def test_preflight_logs_what_loaded_and_what_did_not(caplog):
    async def spawn(config):
        if config["command"] == "broken":
            return FakeProc(stderr="sh: 1: mcp-server-elasticsearch: not found\n")
        return FakeProc(tools=["pods_list", "pods_log"])

    with caplog.at_level(logging.INFO, logger="jean.plugins.mcp_probe"):
        await preflight(
            {"kubernetes": {"command": "npx"}, "elasticsearch": {"command": "broken"}},
            spawn=spawn,
            attempts=1,
            delay=0,
        )

    assert "kubernetes" in caplog.text
    assert "2 tools" in caplog.text
    assert "elasticsearch" in caplog.text
    assert "mcp-server-elasticsearch: not found" in caplog.text


async def test_preflight_ends_with_a_score_naming_both_sides(caplog):
    """One line an operator can read at a glance: how many came up, which ones,
    and which did not -- without reassembling it from the per-server warnings."""

    async def spawn(config):
        if config["command"] == "docker":
            raise FileNotFoundError(2, "No such file or directory", "docker")
        if config["command"] == "broken":
            return FakeProc(stderr="boom\n")
        return FakeProc(tools=["pods_list", "pods_log"])

    with caplog.at_level(logging.INFO, logger="jean.plugins.mcp_probe"):
        results = await preflight(
            {
                "kubectl:kubernetes": {"command": "npx"},
                "elasticsearch:elasticsearch": {"command": "npx"},
                "grafana:grafana": {"command": "docker"},
                "flaky:flaky": {"command": "broken"},
            },
            spawn=spawn,
            attempts=1,
            delay=0,
        )

    summary = [r.message for r in caplog.records if "connected:" in r.message][0]
    assert "2/4 MCP servers connected" in summary
    assert "kubectl:kubernetes (2 tools)" in summary
    assert "elasticsearch:elasticsearch (2 tools)" in summary
    assert "failed: grafana:grafana, flaky:flaky" in summary
    assert sum(r.connected for r in results) == 2


async def test_the_score_says_so_when_every_server_is_up(caplog):
    async def spawn(config):
        return FakeProc(tools=["pods_list"])

    with caplog.at_level(logging.INFO, logger="jean.plugins.mcp_probe"):
        await preflight({"kubectl:kubernetes": {"command": "npx"}}, spawn=spawn, attempts=1)

    summary = [r.message for r in caplog.records if "connected:" in r.message][0]
    assert "1/1 MCP servers connected" in summary
    assert "failed" not in summary

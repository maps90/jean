from __future__ import annotations

import json
from pathlib import Path

from jean.plugins.mcp_config import (
    plugin_mcp_servers,
    remote_servers,
    stdio_servers,
    take_over_plugin_mcp,
)
from jean.ports import ResolvedPlugin


def _plugin(tmp_path, name: str, servers: dict) -> ResolvedPlugin:
    d = tmp_path / name
    d.mkdir()
    (d / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
    return ResolvedPlugin(name=name, path=str(d))


def test_reads_the_stdio_servers_a_plugin_declares(tmp_path):
    p = _plugin(
        tmp_path,
        "kubectl",
        {"kubernetes": {"command": "npx", "args": ["-y", "kubernetes-mcp-server@0.0.64"]}},
    )
    assert plugin_mcp_servers(p) == {
        "kubernetes": {"command": "npx", "args": ["-y", "kubernetes-mcp-server@0.0.64"]}
    }


def test_plugin_without_an_mcp_json_declares_no_servers(tmp_path):
    d = tmp_path / "skills-only"
    d.mkdir()
    assert plugin_mcp_servers(ResolvedPlugin("skills-only", str(d))) == {}


def test_non_stdio_servers_are_not_jeans_to_run(tmp_path):
    p = _plugin(
        tmp_path,
        "remote",
        {"http_one": {"type": "http", "url": "https://x"}, "local": {"command": "npx"}},
    )
    assert list(plugin_mcp_servers(p)) == ["local"]


def test_server_keys_reproduce_the_cli_naming(tmp_path):
    """Verified against the claude CLI in the live pod: the server the CLI names
    `plugin:kubectl:kubernetes` surfaces its tools as
    `mcp__plugin_kubectl_kubernetes__pods_list` -- colons become underscores.
    Proxying must not rename anything, or every skill naming a tool breaks."""
    p = _plugin(tmp_path, "kubectl", {"kubernetes": {"command": "npx"}})

    assert list(stdio_servers({}, [p])) == ["plugin_kubectl_kubernetes"]


def test_stdio_servers_span_mcp_json_and_plugins(tmp_path):
    p = _plugin(tmp_path, "kubectl", {"kubernetes": {"command": "npx"}})

    servers = stdio_servers({"grafana": {"command": "npx"}}, [p])

    assert servers == {
        "grafana": {"command": "npx"},
        "plugin_kubectl_kubernetes": {"command": "npx"},
    }


def test_http_servers_are_left_for_the_cli_to_connect_to():
    """An http server has no process to share -- each session's CLI can open its
    own connection to the same remote, so jean neither runs nor proxies it."""
    extra = {"remote": {"type": "http", "url": "https://x"}, "local": {"command": "npx"}}

    assert stdio_servers(extra, []) == {"local": {"command": "npx"}}
    assert remote_servers(extra) == {"remote": {"type": "http", "url": "https://x"}}


def test_taking_over_a_plugin_stops_the_cli_spawning_its_servers(tmp_path):
    """The CLI spawns whatever it finds in a plugin's .mcp.json, once per session
    per server. Left in place, every server would run twice: jean's shared copy
    and the CLI's private one -- exactly the duplication being removed."""
    p = _plugin(tmp_path, "kubectl", {"kubernetes": {"command": "npx"}})

    take_over_plugin_mcp([p])

    assert not (Path(p.path) / ".mcp.json").exists()
    # ...and jean can still read the config it took over, on this boot and the
    # next one (the marketplace clone is cached across restarts of the process).
    assert plugin_mcp_servers(p) == {"kubernetes": {"command": "npx"}}


def test_taking_over_is_idempotent(tmp_path):
    p = _plugin(tmp_path, "kubectl", {"kubernetes": {"command": "npx"}})

    take_over_plugin_mcp([p])
    take_over_plugin_mcp([p])  # a cached clone: already taken over

    assert list(stdio_servers({}, [p])) == ["plugin_kubectl_kubernetes"]

from __future__ import annotations

import json

from jean.plugins.mcp_config import (
    extra_mcp_tool_patterns,
    plugin_mcp_servers,
    plugin_tool_patterns,
    probeable_servers,
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


def test_non_stdio_servers_are_ignored(tmp_path):
    p = _plugin(
        tmp_path,
        "remote",
        {"http_one": {"type": "http", "url": "https://x"}, "local": {"command": "npx"}},
    )
    assert list(plugin_mcp_servers(p)) == ["local"]


def test_tool_pattern_matches_the_cli_naming(tmp_path):
    """Verified against the claude CLI in the live pod: the server the CLI names
    `plugin:kubectl:kubernetes` surfaces its tools as
    `mcp__plugin_kubectl_kubernetes__pods_list` -- colons become underscores."""
    p = _plugin(tmp_path, "kubectl", {"kubernetes": {"command": "npx"}})
    assert plugin_tool_patterns([p]) == ["mcp__plugin_kubectl_kubernetes__*"]


def test_tool_patterns_cover_every_server_of_every_plugin(tmp_path):
    a = _plugin(tmp_path, "kubectl", {"kubernetes": {"command": "npx"}})
    b = _plugin(tmp_path, "grafana", {"grafana": {"command": "npx"}, "loki": {"command": "npx"}})
    assert plugin_tool_patterns([a, b]) == [
        "mcp__plugin_kubectl_kubernetes__*",
        "mcp__plugin_grafana_grafana__*",
        "mcp__plugin_grafana_loki__*",
    ]


def test_servers_from_mcp_json_use_the_plain_prefix():
    assert extra_mcp_tool_patterns({"grafana": {"command": "npx"}}) == ["mcp__grafana__*"]


def test_probeable_servers_span_mcp_json_and_plugins(tmp_path):
    p = _plugin(tmp_path, "kubectl", {"kubernetes": {"command": "npx"}})

    servers = probeable_servers({"grafana": {"command": "npx"}}, [p])

    assert servers == {
        "grafana": {"command": "npx"},
        "kubectl:kubernetes": {"command": "npx"},
    }


def test_probeable_servers_skip_what_jean_cannot_spawn(tmp_path):
    """An http server in mcp.json has no command to run -- probing it is not
    jean's job, and must not break boot for the servers it can check."""
    servers = probeable_servers({"remote": {"type": "http", "url": "https://x"}}, [])

    assert servers == {}

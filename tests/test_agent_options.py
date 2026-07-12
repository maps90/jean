from __future__ import annotations

import json
import logging

from jean.agent_options import build_agent_options
from jean.config import Settings
from jean.ports import ResolvedPlugin


def _settings(monkeypatch):
    monkeypatch.setenv("JEAN_SLACK_BOT_TOKEN", "xoxb")
    monkeypatch.setenv("JEAN_SLACK_APP_TOKEN", "xapp")
    return Settings.load()


def test_merges_slack_and_external_mcp(monkeypatch):
    opts = build_agent_options(
        persona_text="I am jean.",
        slack_server={"_": "slack"},
        slack_tool_names=["mcp__jean_slack__reply"],
        extra_mcp={"kubernetes": {"command": "npx"}},
        plugins=[ResolvedPlugin("grafana", "/opt/mp/plugins/grafana")],
        settings=_settings(monkeypatch),
        resume=None,
    )
    assert opts.mcp_servers["jean_slack"] == {"_": "slack"}
    assert opts.mcp_servers["kubernetes"] == {"command": "npx"}
    assert opts.plugins == [{"type": "local", "path": "/opt/mp/plugins/grafana"}]
    assert opts.skills == "all"
    assert opts.strict_mcp_config is False


def test_plugin_tools_are_allowed_by_server_not_by_a_bare_wildcard(monkeypatch, tmp_path):
    """`mcp__*` is rejected by the CLI ("Wildcard tool name mcp__* is not
    supported in allow rules") and the rule is dropped, which left every plugin
    tool unreachable in production. The allow pattern must name its server."""
    plugin = tmp_path / "kubectl"
    plugin.mkdir()
    (plugin / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"kubernetes": {"command": "npx"}}})
    )

    opts = build_agent_options(
        persona_text="I am jean.",
        slack_server={"_": "slack"},
        slack_tool_names=["mcp__jean_slack__reply"],
        extra_mcp={"grafana": {"command": "npx"}},
        plugins=[ResolvedPlugin("kubectl", str(plugin))],
        settings=_settings(monkeypatch),
        resume=None,
    )

    assert "mcp__*" not in opts.allowed_tools
    assert "mcp__plugin_kubectl_kubernetes__*" in opts.allowed_tools
    assert "mcp__grafana__*" in opts.allowed_tools
    assert "mcp__jean_slack__reply" in opts.allowed_tools


def test_no_plugins_no_extra_mcp(monkeypatch):
    opts = build_agent_options(
        persona_text="I am jean.",
        slack_server={"_": "slack"},
        slack_tool_names=["mcp__jean_slack__reply"],
        extra_mcp={},
        plugins=[],
        settings=_settings(monkeypatch),
        resume="sess-123",
    )
    assert list(opts.mcp_servers) == ["jean_slack"]
    assert opts.plugins == []
    assert opts.resume == "sess-123"


def test_agent_name_reaches_the_system_prompt(monkeypatch):
    opts = build_agent_options(
        persona_text="Name: Anya",
        agent_name="Anya",
        slack_server={"_": "slack"},
        slack_tool_names=["mcp__jean_slack__reply"],
        extra_mcp={},
        plugins=[],
        settings=_settings(monkeypatch),
        resume=None,
    )
    assert "You are Anya," in opts.system_prompt
    assert "You are jean," not in opts.system_prompt


def test_cli_stderr_is_routed_to_the_logger(monkeypatch, caplog):
    """Without a stderr callback the SDK leaves the CLI child's stderr
    unpiped, and a startup failure surfaces only as ProcessError(stderr="Check
    stderr output for details") -- the actual reason is unreadable."""
    opts = build_agent_options(
        persona_text="I am jean.",
        slack_server={"_": "slack"},
        slack_tool_names=["mcp__jean_slack__reply"],
        extra_mcp={},
        plugins=[],
        settings=_settings(monkeypatch),
        resume=None,
    )
    assert opts.stderr is not None

    with caplog.at_level(logging.WARNING, logger="jean.agent_options"):
        opts.stderr("No conversation found with session ID: abc\n")

    assert "No conversation found with session ID: abc" in caplog.text

from __future__ import annotations

import logging

from claude_agent_sdk import PermissionResultAllow

from jean.agent_options import build_agent_options
from jean.config import Settings
from jean.ports import ResolvedPlugin


async def _allow(tool_name, tool_input, context):
    """Stands in for the Slack approval hook; build_can_use_tool is covered in
    tests/test_tool_permission.py."""
    return PermissionResultAllow()


def _settings(monkeypatch):
    monkeypatch.setenv("JEAN_SLACK_BOT_TOKEN", "xoxb")
    monkeypatch.setenv("JEAN_SLACK_APP_TOKEN", "xapp")
    return Settings.load()


def test_merges_slack_and_proxied_mcp(monkeypatch):
    opts = build_agent_options(
        persona_text="I am jean.",
        slack_server={"_": "slack"},
        slack_tool_names=["mcp__jean_slack__reply"],
        mcp_servers={"kubernetes": {"_": "proxy"}},
        plugins=[ResolvedPlugin("grafana", "/opt/mp/plugins/grafana")],
        settings=_settings(monkeypatch),
        can_use_tool=_allow,
        resume=None,
    )
    assert opts.mcp_servers["jean_slack"] == {"_": "slack"}
    assert opts.mcp_servers["kubernetes"] == {"_": "proxy"}
    assert opts.plugins == [{"type": "local", "path": "/opt/mp/plugins/grafana"}]
    assert opts.skills == "all"
    assert opts.strict_mcp_config is False


def test_tools_are_allowed_by_server_not_by_a_bare_wildcard(monkeypatch):
    """`mcp__*` is rejected by the CLI ("Wildcard tool name mcp__* is not
    supported in allow rules") and the rule is dropped, which left every plugin
    tool unreachable in production. The allow pattern must name its server."""
    opts = build_agent_options(
        persona_text="I am jean.",
        slack_server={"_": "slack"},
        slack_tool_names=["mcp__jean_slack__reply"],
        mcp_servers={"plugin_kubectl_kubernetes": {"_": "proxy"}, "grafana": {"_": "proxy"}},
        plugins=[ResolvedPlugin("kubectl", "/opt/mp/plugins/kubectl")],
        settings=_settings(monkeypatch),
        can_use_tool=_allow,
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
        mcp_servers={},
        plugins=[],
        settings=_settings(monkeypatch),
        can_use_tool=_allow,
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
        mcp_servers={},
        plugins=[],
        settings=_settings(monkeypatch),
        can_use_tool=_allow,
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
        mcp_servers={},
        plugins=[],
        settings=_settings(monkeypatch),
        can_use_tool=_allow,
        resume=None,
    )
    assert opts.stderr is not None

    with caplog.at_level(logging.WARNING, logger="jean.agent_options"):
        opts.stderr("No conversation found with session ID: abc\n")

    assert "No conversation found with session ID: abc" in caplog.text

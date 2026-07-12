from __future__ import annotations

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

from __future__ import annotations

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

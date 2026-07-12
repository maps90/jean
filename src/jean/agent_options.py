from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, PermissionResultAllow

from jean.config import Settings
from jean.persona.identity import DEFAULT_AGENT_NAME, compose_system_prompt
from jean.ports import ResolvedPlugin

logger = logging.getLogger(__name__)


def _log_cli_stderr(line: str) -> None:
    """Surface the claude CLI child's stderr in jean's logs.

    The SDK pipes that stderr *only* when this callback is set; otherwise the
    child inherits ours and a startup failure arrives as a bare ProcessError
    whose `stderr` is the literal string "Check stderr output for details"
    (subprocess_cli.py) -- i.e. the one thing that says why the CLI exited is
    the thing you cannot see. Everything the CLI writes here is a diagnostic,
    so log it as a warning rather than dropping it.
    """
    line = line.rstrip()
    if line:
        logger.warning("claude-cli: %s", line)


async def _allow_all_tools(
    tool_name: str, tool_input: dict[str, Any], context: Any
) -> PermissionResultAllow:
    del tool_name, tool_input, context
    return PermissionResultAllow()


def build_agent_options(
    *,
    persona_text: str,
    slack_server: Any,
    slack_tool_names: list[str],
    extra_mcp: dict[str, Any],
    plugins: list[ResolvedPlugin],
    settings: Settings,
    resume: str | None,
    agent_name: str = DEFAULT_AGENT_NAME,
) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=compose_system_prompt(persona_text, name=agent_name),
        mcp_servers={"jean_slack": slack_server, **extra_mcp},
        allowed_tools=[*slack_tool_names, "mcp__*"],
        plugins=[{"type": "local", "path": p.path} for p in plugins],
        skills="all",
        strict_mcp_config=False,
        permission_mode=settings.permission_mode,
        can_use_tool=_allow_all_tools,
        resume=resume,
        model=settings.model,
        cwd=str(settings.home / "workspaces"),
        stderr=_log_cli_stderr,
    )


# Note (spike): `allowed_tools` includes `"mcp__*"` so plugin/external MCP
# tools are reachable. Before merging, run jean against one real plugin (e.g.
# `grafana`) and confirm its `mcp__grafana__*` tools are callable; if the CLI
# requires exact names instead of the wildcard, replace `"mcp__*"` with the
# per-server patterns discovered at resolve time.

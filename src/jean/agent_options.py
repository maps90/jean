from __future__ import annotations

import logging
from typing import Any, Protocol

from claude_agent_sdk import (
    CanUseTool,
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
)

from jean.approval.policy import deny_reason, summarize
from jean.config import Settings
from jean.persona.identity import DEFAULT_AGENT_NAME, compose_system_prompt
from jean.plugins.mcp_proxy import proxy_tool_patterns
from jean.ports import ApprovalDecision, ResolvedPlugin

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


class _Gate(Protocol):
    async def request(self, channel: str, thread_ts: str, summary: str) -> ApprovalDecision: ...


def build_can_use_tool(gate: _Gate, *, channel: str, thread_ts: str) -> CanUseTool:
    """The SDK's permission hook, answered by a human clicking a Slack button.

    The CLI calls this for every tool it will not auto-allow -- i.e. everything
    outside `allowed_tools` (jean's own Slack tools and the MCP servers) and
    outside its read-only set. So Bash, Write and Edit reach here and block
    until an approver decides; a `kubectl get` through the kubernetes MCP
    server does not.

    Only reachable when `permission_mode` is a mode that *asks*: under
    `bypassPermissions` the CLI skips its permission system entirely and this
    is never called, which is why that is no longer the default (config.py).

    channel/thread_ts are bound per session rather than read from the shared
    RoutingContext: this awaits a human for up to `approval_ttl`, and any turn
    starting on another thread meanwhile would repoint that shared state --
    posting this approval into the wrong thread, in front of the wrong people.
    """

    async def can_use_tool(
        tool_name: str, tool_input: dict[str, Any], context: Any
    ) -> PermissionResultAllow | PermissionResultDeny:
        del context
        decision = await gate.request(channel, thread_ts, summarize(tool_name, tool_input))
        if decision.approved:
            logger.info("approved: %s in %s/%s by %s", tool_name, channel, thread_ts, decision.by)
            return PermissionResultAllow()
        logger.info("denied: %s in %s/%s by %s", tool_name, channel, thread_ts, decision.by)
        # interrupt=False: the tool does not run, but the turn lives, so the
        # agent can tell the thread it was denied instead of dying silently.
        return PermissionResultDeny(message=deny_reason(decision), interrupt=False)

    return can_use_tool


def build_agent_options(
    *,
    persona_text: str,
    slack_server: Any,
    slack_tool_names: list[str],
    mcp_servers: dict[str, Any],
    plugins: list[ResolvedPlugin],
    settings: Settings,
    resume: str | None,
    can_use_tool: CanUseTool,
    permission_mode: str | None = None,
    agent_name: str = DEFAULT_AGENT_NAME,
) -> ClaudeAgentOptions:
    """`mcp_servers` are jean's in-process proxies (plugins/mcp_proxy.py) plus any
    remote http servers -- never a stdio config. A stdio entry here would have the
    CLI fork its own copy of that server for every session, which is what the
    proxy exists to prevent."""
    return ClaudeAgentOptions(
        system_prompt=compose_system_prompt(persona_text, name=agent_name),
        mcp_servers={"jean_slack": slack_server, **mcp_servers},
        allowed_tools=[*slack_tool_names, *proxy_tool_patterns(mcp_servers)],
        plugins=[{"type": "local", "path": p.path} for p in plugins],
        skills="all",
        strict_mcp_config=False,
        # The thread's own /mode wins over the deployment default when it set one.
        permission_mode=permission_mode or settings.permission_mode,
        can_use_tool=can_use_tool,
        resume=resume,
        model=settings.model,
        cwd=str(settings.home / "workspaces"),
        stderr=_log_cli_stderr,
    )

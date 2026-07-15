from __future__ import annotations

import logging
from typing import Any, Protocol

from claude_agent_sdk import (
    CanUseTool,
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    PermissionUpdate,
)
from claude_agent_sdk.types import PermissionRuleValue

from jean.approval.policy import deny_reason, summarize
from jean.approval.risk import DENY_MESSAGE, Risk, classify_risk
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
    """The SDK's permission hook. A deterministic classifier decides risk; only
    RISKY calls reach a human.

    Runs under `default` permission_mode, so the CLI calls this for every tool
    outside `allowed_tools` (jean's Slack tools + the MCP proxies) and outside
    its read-only set -- i.e. Bash/Write/Edit and mutating plugin-MCP calls.

    - SAFE  -> allow silently. Routine work never blocks.
    - DENY  -> refuse in code; never prompt a human.
    - RISKY -> ask an approver. "Always allow" adds a session-scoped rule so a
      repeated pattern stops asking.

    channel/thread_ts are bound per session (not read from a process-wide slot)
    because this awaits a human and a turn on another thread must not repoint it.
    """

    async def can_use_tool(
        tool_name: str, tool_input: dict[str, Any], context: Any
    ) -> PermissionResultAllow | PermissionResultDeny:
        del context

        risk = classify_risk(tool_name, tool_input)
        if risk is Risk.SAFE:
            return PermissionResultAllow()
        if risk is Risk.DENY:
            logger.info("hard-denied: %s in %s/%s", tool_name, channel, thread_ts)
            return PermissionResultDeny(message=DENY_MESSAGE, interrupt=False)

        # RISKY -> a human decides.
        decision: ApprovalDecision = await gate.request(
            channel, thread_ts, summarize(tool_name, tool_input)
        )
        if not decision.approved:
            logger.info("denied: %s in %s/%s by %s", tool_name, channel, thread_ts, decision.by)
            # interrupt=False: the tool does not run, but the turn lives, so the
            # agent can tell the thread it was denied instead of dying silently.
            return PermissionResultDeny(message=deny_reason(decision), interrupt=False)

        if decision.scope == "always":
            logger.info(
                "always-allowed: %s in %s/%s by %s", tool_name, channel, thread_ts, decision.by
            )
            return PermissionResultAllow(
                updated_permissions=[
                    PermissionUpdate(
                        type="addRules",
                        rules=[PermissionRuleValue(tool_name=tool_name, rule_content=None)],
                        behavior="allow",
                        destination="session",
                    )
                ]
            )
        logger.info("approved: %s in %s/%s by %s", tool_name, channel, thread_ts, decision.by)
        return PermissionResultAllow()

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

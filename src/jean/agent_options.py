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

    channel/thread_ts are bound per session rather than read from a process-wide
    slot at call time: this awaits a human for up to `approval_ttl`, and any turn
    starting on another thread meanwhile would repoint that shared state --
    posting this approval into the wrong thread, in front of the wrong people.
    The per-session `jean_slack` MCP server (slack/mcp.py) is bound the same way,
    for the same reason.
    """

    seen_exit_plan = False  # log the raw ExitPlanMode input once, to confirm its shape

    async def can_use_tool(
        tool_name: str, tool_input: dict[str, Any], context: Any
    ) -> PermissionResultAllow | PermissionResultDeny:
        nonlocal seen_exit_plan
        del context

        # Under the default plan mode the CLI keeps the agent read-only until it
        # presents its plan via ExitPlanMode -- so THIS is the single approval:
        # the human reads the whole plan and clicks once. Approving it must both
        # let the agent leave plan mode AND stop prompting for the rest of the
        # turn, so the approved steps run unattended (what the human just agreed
        # to). We do that by flipping the session to bypassPermissions here;
        # JeanSession re-arms `plan` before the next turn, so the approval binds
        # to this one plan and the next request is planned and approved afresh.
        if tool_name == "ExitPlanMode":
            if not seen_exit_plan:
                # The exact input key is not documented; confirm it against the
                # real CLI once, so a rename shows up in the logs instead of
                # silently degrading to the JSON fallback in summarize().
                logger.info("ExitPlanMode input keys: %s", sorted(tool_input))
                seen_exit_plan = True
            decision = await gate.request(channel, thread_ts, summarize(tool_name, tool_input))
            if decision.approved:
                logger.info("plan approved in %s/%s by %s", channel, thread_ts, decision.by)
                return PermissionResultAllow(
                    updated_permissions=[
                        PermissionUpdate(
                            type="setMode", mode="bypassPermissions", destination="session"
                        )
                    ]
                )
            logger.info("plan denied in %s/%s by %s", channel, thread_ts, decision.by)
            # interrupt=False: stays in plan mode so the agent can report the
            # denial; a later attempt re-plans rather than the thread dying.
            return PermissionResultDeny(message=deny_reason(decision), interrupt=False)

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

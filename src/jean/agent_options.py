from __future__ import annotations

from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, PermissionResultAllow

from jean.config import Settings
from jean.persona.identity import compose_system_prompt
from jean.ports import ResolvedPlugin


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
) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=compose_system_prompt(persona_text),
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
    )


# Note (spike): `allowed_tools` includes `"mcp__*"` so plugin/external MCP
# tools are reachable. Before merging, run jean against one real plugin (e.g.
# `grafana`) and confirm its `mcp__grafana__*` tools are callable; if the CLI
# requires exact names instead of the wildcard, replace `"mcp__*"` with the
# per-server patterns discovered at resolve time.

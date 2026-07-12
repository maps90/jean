from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jean.ports import ResolvedPlugin


def plugin_mcp_servers(plugin: ResolvedPlugin) -> dict[str, dict[str, Any]]:
    """The stdio MCP servers a plugin declares in its own `.mcp.json`.

    Only stdio servers (those with a `command`) are returned: they are the ones
    jean can probe by spawning them, and the ones that fail when the command
    cannot be resolved at runtime.
    """
    path = Path(plugin.path) / ".mcp.json"
    if not path.exists():
        return {}
    servers = json.loads(path.read_text()).get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"{path}: 'mcpServers' must be an object")
    return {name: cfg for name, cfg in servers.items() if "command" in cfg}


def plugin_tool_patterns(plugins: list[ResolvedPlugin]) -> list[str]:
    """Allow-list patterns for the tools a plugin's MCP servers expose.

    The CLI names a plugin's server `plugin:<plugin>:<server>` and exposes its
    tools as `mcp__plugin_<plugin>_<server>__<tool>` -- colons become
    underscores (verified against the running CLI: `plugin:kubectl:kubernetes`
    serves `mcp__plugin_kubectl_kubernetes__pods_list`).

    A bare `mcp__*` does NOT work here: the CLI rejects it outright
    ("Wildcard tool name mcp__* is not supported in allow rules") and drops the
    rule, so every plugin tool stays unreachable. A glob is only allowed in the
    tool position, after a literal server prefix.
    """
    return [
        f"mcp__plugin_{plugin.name}_{server}__*"
        for plugin in plugins
        for server in plugin_mcp_servers(plugin)
    ]


def probeable_servers(
    extra_mcp: dict[str, Any], plugins: list[ResolvedPlugin]
) -> dict[str, dict[str, Any]]:
    """Every stdio MCP server jean can spawn itself, keyed by a name for the logs.

    Servers jean cannot run (an http one in `mcp.json`, say) are not jean's to
    check and are left out rather than failing the boot probe.
    """
    servers = {name: cfg for name, cfg in extra_mcp.items() if "command" in cfg}
    for plugin in plugins:
        for server, cfg in plugin_mcp_servers(plugin).items():
            servers[f"{plugin.name}:{server}"] = cfg
    return servers


def extra_mcp_tool_patterns(servers: dict[str, Any]) -> list[str]:
    """Allow-list patterns for servers configured directly in jean's `mcp.json`.

    These are not plugin-scoped, so they carry the plain `mcp__<server>__`
    prefix.
    """
    return [f"mcp__{name}__*" for name in servers]

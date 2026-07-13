from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool

from jean.plugins.mcp_client import McpClient

logger = logging.getLogger(__name__)


def build_proxy_tools(client: McpClient) -> list[SdkMcpTool]:
    """Re-expose one upstream server's tools as in-process SDK tools.

    The upstream's own name, description and JSON schema are forwarded verbatim,
    so the agent sees exactly the tool it would have seen had the CLI spawned the
    server itself -- the only thing that changes is who owns the process.
    """
    tools: list[SdkMcpTool] = []
    for spec in client.tools:
        name = spec["name"]

        # `name=name` binds per iteration: a bare closure over `name` would leave
        # every tool calling whichever one the loop ended on.
        async def handler(args: dict[str, Any], *, name: str = name) -> dict[str, Any]:
            return await client.call(name, args)

        schema = spec.get("inputSchema") or {"type": "object", "properties": {}}
        tools.append(tool(name, spec.get("description", ""), schema)(handler))
    return tools


def build_proxy_servers(clients: list[McpClient]) -> dict[str, Any]:
    """One in-process SDK MCP server per upstream, keyed by the server key.

    The key is what names the tool the agent calls (`mcp__<key>__<tool>`), so it
    is chosen to be byte-identical to the name the CLI used when it spawned the
    server itself -- `plugin_kubectl_kubernetes` for a plugin's server. Anything
    that already refers to these tools (an oka-skills skill's allowed-tools, a
    prompt naming `mcp__plugin_kubectl_kubernetes__pods_list`) keeps working.
    """
    servers: dict[str, Any] = {}
    for client in clients:
        tools = build_proxy_tools(client)
        servers[client.name] = create_sdk_mcp_server(client.name, tools=tools)
        logger.info("mcp %s: proxying %d tool(s) in-process", client.name, len(tools))
    return servers


def proxy_tool_patterns(servers: dict[str, Any]) -> list[str]:
    """Allow-list patterns for the proxied tools.

    A bare `mcp__*` does NOT work: the CLI rejects it outright ("Wildcard tool
    name mcp__* is not supported in allow rules") and drops the rule, leaving
    every tool unreachable. A glob is only allowed after a literal server prefix.
    """
    return [f"mcp__{key}__*" for key in servers]

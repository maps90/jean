from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from jean.plugins.env_refs import expand_config
from jean.ports import ResolvedPlugin

logger = logging.getLogger(__name__)

# Where a plugin's MCP config is parked once jean takes its servers over. The CLI
# spawns whatever it finds in a plugin's `.mcp.json` -- once per session, per
# server -- so leaving the file in place would run every server twice: jean's
# shared copy, and the CLI's private one. Renaming is reversible and leaves the
# original readable, which deleting would not.
DISABLED_SUFFIX = ".jean-owned"


def _mcp_json(plugin: ResolvedPlugin) -> Path | None:
    """The plugin's MCP config, whether or not jean has taken it over already
    (the marketplace clone is cached, so a second boot finds the renamed file)."""
    for name in (".mcp.json", f".mcp.json{DISABLED_SUFFIX}"):
        path = Path(plugin.path) / name
        if path.exists():
            return path
    return None


def plugin_mcp_servers(plugin: ResolvedPlugin) -> dict[str, dict[str, Any]]:
    """The stdio MCP servers a plugin declares.

    Only stdio servers (those with a `command`) are returned: they are the ones
    jean runs itself. An http/sse server has no process to share, so the CLI can
    hold its own connection to it and jean leaves it alone.
    """
    path = _mcp_json(plugin)
    if path is None:
        return {}
    servers = json.loads(path.read_text()).get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"{path}: 'mcpServers' must be an object")
    return {name: cfg for name, cfg in servers.items() if "command" in cfg}


def stdio_servers(extra_mcp: dict[str, Any], plugins: list[ResolvedPlugin]) -> dict[str, Any]:
    """Every stdio MCP server jean runs itself, keyed by its agent-facing name.

    The key becomes the tool prefix the agent sees (`mcp__<key>__<tool>`), so it
    reproduces exactly what the CLI called the server when the CLI still spawned
    it: `plugin_kubectl_kubernetes` for a plugin's server (the CLI names it
    `plugin:kubectl:kubernetes` and converts the colons), and the plain name for
    one declared in jean's own mcp.json. Keeping those ids stable means every
    skill and prompt that already refers to a tool keeps working.
    """
    servers = {name: cfg for name, cfg in extra_mcp.items() if "command" in cfg}
    for plugin in plugins:
        for server, cfg in plugin_mcp_servers(plugin).items():
            servers[f"plugin_{plugin.name}_{server}"] = cfg
    return servers


def remote_servers(extra_mcp: dict[str, Any]) -> dict[str, Any]:
    """The http/sse servers in mcp.json, with their `${VAR}` references resolved.

    There is no child process to share: every session's CLI just opens its own
    connection to the same remote server, which is what jean wants anyway.

    Expanded here rather than left to the CLI because the SDK ships these as an
    inline `--mcp-config` JSON blob, not as a .mcp.json on disk -- so whether the
    CLI would expand them is undocumented, and a bearer token is not the thing to
    find that out with. Expanding is idempotent: if the CLI expands too, jean has
    already substituted and there is nothing left for it to find.

    Strict (env_refs.expand_config), so an unset var raises at boot. Registering
    an HTTP API as the MCP server it already is is precisely what keeps it off the
    Bash/curl path, where every call costs a human an approval click -- and a
    silently blank credential would 401 every call and send the agent right back
    to curl.
    """
    return {
        name: expand_config(cfg, server=name)
        for name, cfg in extra_mcp.items()
        if "command" not in cfg
    }


def take_over_plugin_mcp(plugins: list[ResolvedPlugin]) -> None:
    """Stop the CLI from spawning a plugin's MCP servers behind jean's back.

    jean already runs each of them once and proxies the tools in-process. If the
    plugin's `.mcp.json` stayed where it is, the CLI would *also* fork its own
    copy of every server for every session -- which is the duplication this whole
    arrangement exists to remove -- and the agent would see each tool twice.
    """
    for plugin in plugins:
        path = Path(plugin.path) / ".mcp.json"
        if not path.exists():
            continue
        path.rename(path.with_suffix(f".json{DISABLED_SUFFIX}"))
        logger.info("mcp: took over %s's servers; the CLI will not spawn its own", plugin.name)

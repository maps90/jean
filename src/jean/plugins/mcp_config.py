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


def _declared_servers(plugin: ResolvedPlugin) -> dict[str, dict[str, Any]]:
    """Every MCP server in a plugin's .mcp.json, stdio and http alike."""
    path = _mcp_json(plugin)
    if path is None:
        return {}
    servers = json.loads(path.read_text()).get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"{path}: 'mcpServers' must be an object")
    return servers


def plugin_mcp_servers(plugin: ResolvedPlugin) -> dict[str, dict[str, Any]]:
    """The stdio MCP servers a plugin declares -- the ones jean runs itself."""
    return {name: cfg for name, cfg in _declared_servers(plugin).items() if "command" in cfg}


def plugin_remote_servers(plugin: ResolvedPlugin) -> dict[str, dict[str, Any]]:
    """The http/sse MCP servers a plugin declares.

    These have no process to share, so jean does not run them -- but it must still
    *register* them, and this function is why. Until it existed, a plugin's http
    server was registered by nobody: plugin_mcp_servers() filtered it out for
    having no `command`, remote_servers() only ever read jean's own mcp.json, and
    take_over_plugin_mcp() then renamed the file so the CLI could not reach it
    either. The tools simply did not exist, and the agent fell back to curl-ing the
    endpoint through Bash -- one approval click per call, and no schemas to work
    from, so it guessed at tool names too.

    Registering them is also what makes them *free*: `allowed_tools` is built from
    the servers jean knows about (agent_options.build_agent_options), so a server
    jean has never heard of gets no `mcp__<key>__*` allow rule, and every call to
    it would hit the approval gate even if the CLI had connected to it.
    """
    return {name: cfg for name, cfg in _declared_servers(plugin).items() if "command" not in cfg}


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


def remote_servers(extra_mcp: dict[str, Any], plugins: list[ResolvedPlugin]) -> dict[str, Any]:
    """Every http/sse MCP server jean registers, from mcp.json AND from plugins.

    jean does not run these -- there is no process to share, so each session's CLI
    opens its own connection. But jean must still hand them to the CLI, because
    `allowed_tools` is built from the servers jean knows about: an unregistered
    server has no `mcp__<key>__*` allow rule, so every call to it goes to the human
    approval gate. Registering is what makes the tools both *exist* and be free.

    **The key is the name the server declares**, whether it came from jean's mcp.json
    or a plugin's .mcp.json, because the key is the agent-facing tool prefix
    (`mcp__<key>__<tool>`). Note this is deliberately NOT the `plugin_<plugin>_<server>`
    form used for stdio: that form exists to reproduce ids the CLI already minted for
    servers it used to spawn, and no such ids exist here. It would also not fit -- a
    gateway proxies tools that are already namespaced, so
    `mcp__plugin_portico_portico__atlassian__getAccessibleAtlassianResources` is 71
    characters against a 64-character tool-name limit, while `mcp__portico__…` is 56.

    `${VAR}` is resolved here rather than left to the CLI because the SDK ships these
    as an inline `--mcp-config` JSON blob, not as a .mcp.json on disk -- so whether the
    CLI would expand them is undocumented, and a bearer token is not the thing to find
    that out with. Expanding is idempotent: if the CLI expands too, jean has already
    substituted and there is nothing left for it to find. Strict, so an unset var with
    no default raises at boot rather than sending `Bearer ` and 401ing every call --
    which would break the tools and send the agent straight back to curl.
    """
    declared: dict[str, Any] = {
        name: cfg for name, cfg in extra_mcp.items() if "command" not in cfg
    }
    sources = {name: "mcp.json" for name in declared}

    for plugin in plugins:
        for name, cfg in plugin_remote_servers(plugin).items():
            if name in declared:
                # The key is a tool prefix, so a duplicate would have one server's
                # tools silently shadow the other's. Name both and refuse.
                raise ValueError(
                    f"two MCP servers both claim the name {name!r}: "
                    f"{sources[name]} and plugin {plugin.name!r}. "
                    f"Tool ids are built from this name, so one would shadow the other."
                )
            declared[name] = cfg
            sources[name] = f"plugin {plugin.name!r}"

    return {name: expand_config(cfg, server=name) for name, cfg in declared.items()}


def take_over_plugin_mcp(plugins: list[ResolvedPlugin]) -> None:
    """Stop the CLI from reaching a plugin's MCP servers behind jean's back.

    jean owns every server the plugin declares: it runs the stdio ones once and
    proxies their tools in-process (mcp_client/mcp_proxy), and it registers the
    http ones with the CLI itself (remote_servers). If the plugin's `.mcp.json`
    stayed where it is, the CLI would *also* fork its own copy of every stdio
    server for every session -- the duplication this whole arrangement exists to
    remove -- and would connect to the http ones a second time under a different
    key, so the agent would see each tool twice, under two names.

    Renaming the file is therefore correct for both kinds -- but only *because*
    jean now registers the http ones. It did not, once: they were filtered out as
    having no `command`, never collected, and then hidden here. That combination
    is what left the agent curl-ing an MCP endpoint through Bash, one approval
    click at a time. Do not reinstate the rename without the registration.
    """
    for plugin in plugins:
        path = Path(plugin.path) / ".mcp.json"
        if not path.exists():
            continue
        path.rename(path.with_suffix(f".json{DISABLED_SUFFIX}"))
        logger.info("mcp: took over %s's servers; the CLI will not spawn its own", plugin.name)

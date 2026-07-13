from __future__ import annotations

import json
from pathlib import Path

import pytest

from jean.plugins.env_refs import MissingEnvVar
from jean.plugins.mcp_config import (
    plugin_mcp_servers,
    remote_servers,
    stdio_servers,
    take_over_plugin_mcp,
)
from jean.ports import ResolvedPlugin


def _plugin(tmp_path, name: str, servers: dict) -> ResolvedPlugin:
    d = tmp_path / name
    d.mkdir()
    (d / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
    return ResolvedPlugin(name=name, path=str(d))


def test_reads_the_stdio_servers_a_plugin_declares(tmp_path):
    p = _plugin(
        tmp_path,
        "kubectl",
        {"kubernetes": {"command": "npx", "args": ["-y", "kubernetes-mcp-server@0.0.64"]}},
    )
    assert plugin_mcp_servers(p) == {
        "kubernetes": {"command": "npx", "args": ["-y", "kubernetes-mcp-server@0.0.64"]}
    }


def test_plugin_without_an_mcp_json_declares_no_servers(tmp_path):
    d = tmp_path / "skills-only"
    d.mkdir()
    assert plugin_mcp_servers(ResolvedPlugin("skills-only", str(d))) == {}


def test_non_stdio_servers_are_not_jeans_to_run(tmp_path):
    p = _plugin(
        tmp_path,
        "remote",
        {"http_one": {"type": "http", "url": "https://x"}, "local": {"command": "npx"}},
    )
    assert list(plugin_mcp_servers(p)) == ["local"]


def test_server_keys_reproduce_the_cli_naming(tmp_path):
    """Verified against the claude CLI in the live pod: the server the CLI names
    `plugin:kubectl:kubernetes` surfaces its tools as
    `mcp__plugin_kubectl_kubernetes__pods_list` -- colons become underscores.
    Proxying must not rename anything, or every skill naming a tool breaks."""
    p = _plugin(tmp_path, "kubectl", {"kubernetes": {"command": "npx"}})

    assert list(stdio_servers({}, [p])) == ["plugin_kubectl_kubernetes"]


def test_stdio_servers_span_mcp_json_and_plugins(tmp_path):
    p = _plugin(tmp_path, "kubectl", {"kubernetes": {"command": "npx"}})

    servers = stdio_servers({"grafana": {"command": "npx"}}, [p])

    assert servers == {
        "grafana": {"command": "npx"},
        "plugin_kubectl_kubernetes": {"command": "npx"},
    }


def test_http_servers_are_left_for_the_cli_to_connect_to():
    """An http server has no process to share -- each session's CLI can open its
    own connection to the same remote, so jean neither runs nor proxies it."""
    extra = {"remote": {"type": "http", "url": "https://x"}, "local": {"command": "npx"}}

    assert stdio_servers(extra, []) == {"local": {"command": "npx"}}
    assert remote_servers(extra, []) == {"remote": {"type": "http", "url": "https://x"}}


def test_taking_over_a_plugin_stops_the_cli_spawning_its_servers(tmp_path):
    """The CLI spawns whatever it finds in a plugin's .mcp.json, once per session
    per server. Left in place, every server would run twice: jean's shared copy
    and the CLI's private one -- exactly the duplication being removed."""
    p = _plugin(tmp_path, "kubectl", {"kubernetes": {"command": "npx"}})

    take_over_plugin_mcp([p])

    assert not (Path(p.path) / ".mcp.json").exists()
    # ...and jean can still read the config it took over, on this boot and the
    # next one (the marketplace clone is cached across restarts of the process).
    assert plugin_mcp_servers(p) == {"kubernetes": {"command": "npx"}}


def test_taking_over_is_idempotent(tmp_path):
    p = _plugin(tmp_path, "kubectl", {"kubernetes": {"command": "npx"}})

    take_over_plugin_mcp([p])
    take_over_plugin_mcp([p])  # a cached clone: already taken over

    assert list(stdio_servers({}, [p])) == ["plugin_kubectl_kubernetes"]


def test_a_plugins_http_server_is_registered_not_dropped(tmp_path, monkeypatch):
    """The bug this file exists to close. A plugin that declares an http MCP server
    used to have it registered by NOBODY: plugin_mcp_servers() filtered it out (no
    `command`), remote_servers() never looked at plugins, and take_over_plugin_mcp()
    then renamed the file away so the CLI could not reach it either. The agent was
    left to curl the endpoint through Bash -- and every Bash call is an approval
    click. Eleven of them, in one thread, to answer one question."""
    monkeypatch.setenv("GW_TOKEN", "sekrit")
    p = _plugin(
        tmp_path,
        "gateway",
        {
            "gw": {
                "type": "http",
                "url": "https://gw.internal/mcp",
                "headers": {"Authorization": "Bearer ${GW_TOKEN}"},
            }
        },
    )

    assert remote_servers({}, [p]) == {
        "gw": {
            "type": "http",
            "url": "https://gw.internal/mcp",
            "headers": {"Authorization": "Bearer sekrit"},
        }
    }


def test_a_plugins_http_server_is_keyed_by_the_name_it_declares(tmp_path):
    """The key is the agent-facing tool prefix (`mcp__<key>__<tool>`), so it is the
    plugin's declared name, NOT the `plugin_<plugin>_<server>` form used for stdio.

    That form exists to reproduce ids the CLI already minted for servers it used to
    spawn. No such ids exist here -- these tools have never worked -- and the prefix
    budget is real: a gateway proxies tools that are already namespaced, so
    `mcp__plugin_portico_portico__atlassian__getAccessibleAtlassianResources` is 71
    characters against a 64-character limit. `mcp__portico__…` fits; the long form
    does not."""
    p = _plugin(tmp_path, "portico", {"portico": {"type": "http", "url": "https://x/mcp"}})

    assert list(remote_servers({}, [p])) == ["portico"]


def test_a_plugins_stdio_server_still_goes_through_the_proxy(tmp_path):
    """Regression: http changes nothing for stdio. jean still runs those itself."""
    p = _plugin(
        tmp_path,
        "kubectl",
        {"kubernetes": {"command": "npx"}, "gw": {"type": "http", "url": "https://x"}},
    )

    assert list(stdio_servers({}, [p])) == ["plugin_kubectl_kubernetes"]
    assert list(remote_servers({}, [p])) == ["gw"]


def test_two_plugins_claiming_the_same_server_name_fail_at_boot(tmp_path):
    """The key is a tool prefix, so a duplicate would have one server's tools
    silently shadow the other's. Refuse, naming both, rather than pick a winner."""
    a = _plugin(tmp_path, "alpha", {"gw": {"type": "http", "url": "https://a"}})
    b = _plugin(tmp_path, "beta", {"gw": {"type": "http", "url": "https://b"}})

    with pytest.raises(ValueError, match="gw"):
        remote_servers({}, [a, b])


def test_a_remote_servers_credential_comes_from_the_environment(monkeypatch):
    """Registering an HTTP API as the MCP server it already is is what keeps it
    off the Bash/curl path -- where every single call costs a human an approval
    click. Its token reaches it from the env, so the mounted mcp.json holds no
    second copy of a credential to rotate."""
    monkeypatch.setenv("PORTICO_ACCESS_TOKEN", "sekrit")
    extra = {
        "portico": {
            "type": "http",
            "url": "https://portico.int.okadoc.net/mcp",
            "headers": {"Authorization": "Bearer ${PORTICO_ACCESS_TOKEN}"},
        }
    }

    assert remote_servers(extra, []) == {
        "portico": {
            "type": "http",
            "url": "https://portico.int.okadoc.net/mcp",
            "headers": {"Authorization": "Bearer sekrit"},
        }
    }


def test_a_remote_server_missing_its_credential_fails_at_boot(monkeypatch):
    """remote_servers() runs at boot (server.py). Better a crashloop naming the
    variable than a jean that boots clean and 401s on every call -- which sends
    the agent back to curl, i.e. back to one approval click per call."""
    monkeypatch.delenv("PORTICO_ACCESS_TOKEN", raising=False)
    extra = {
        "portico": {
            "type": "http",
            "url": "https://portico.int.okadoc.net/mcp",
            "headers": {"Authorization": "Bearer ${PORTICO_ACCESS_TOKEN}"},
        }
    }

    with pytest.raises(MissingEnvVar, match="PORTICO_ACCESS_TOKEN"):
        remote_servers(extra, [])


def test_a_stdio_servers_config_is_not_expanded_here(monkeypatch):
    """stdio configs are expanded at spawn (mcp_stdio), and only in their `env`
    block. remote_servers() must not reach into them at all -- an unset var in a
    stdio config must not take the whole pod down."""
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    extra = {"local": {"command": "npx", "args": ["${NOT_SET_ANYWHERE}"]}}

    assert remote_servers(extra, []) == {}
    assert stdio_servers(extra, []) == {
        "local": {"command": "npx", "args": ["${NOT_SET_ANYWHERE}"]}
    }

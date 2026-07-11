from __future__ import annotations

import json

import pytest

from jean.plugins.manifest import load_mcp_config, load_plugin_manifest
from jean.ports import PluginRef


def test_load_plugin_manifest_parses_entries(tmp_path):
    p = tmp_path / "jean.json"
    p.write_text(
        json.dumps(
            {
                "plugins": [
                    {
                        "marketplace": "git@github.com:OkadocTech/oka-skills.git",
                        "plugin": "grafana",
                        "ref": "main",
                    },
                ]
            }
        )
    )
    assert load_plugin_manifest(p) == [
        PluginRef("git@github.com:OkadocTech/oka-skills.git", "grafana", "main"),
    ]


def test_load_plugin_manifest_missing_file_is_empty(tmp_path):
    assert load_plugin_manifest(tmp_path / "absent.json") == []


def test_load_plugin_manifest_rejects_missing_field(tmp_path):
    p = tmp_path / "jean.json"
    p.write_text(json.dumps({"plugins": [{"marketplace": "x", "plugin": "grafana"}]}))
    with pytest.raises(ValueError):
        load_plugin_manifest(p)


def test_load_plugin_manifest_rejects_non_object(tmp_path):
    p = tmp_path / "jean.json"
    p.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(ValueError):
        load_plugin_manifest(p)


def test_load_mcp_config_rejects_non_object(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(ValueError):
        load_mcp_config(p)


def test_load_mcp_config_returns_servers_map(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(
        json.dumps({"mcpServers": {"kubernetes": {"command": "npx", "args": ["-y", "x"]}}})
    )
    assert load_mcp_config(p) == {"kubernetes": {"command": "npx", "args": ["-y", "x"]}}


def test_load_mcp_config_missing_file_is_empty(tmp_path):
    assert load_mcp_config(tmp_path / "absent.json") == {}

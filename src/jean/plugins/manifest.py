from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jean.ports import PluginRef


def load_plugin_manifest(path: Path) -> list[PluginRef]:
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    entries = data.get("plugins", [])
    refs: list[PluginRef] = []
    for e in entries:
        try:
            refs.append(PluginRef(e["marketplace"], e["plugin"], e["ref"]))
        except (KeyError, TypeError) as exc:
            raise ValueError(f"invalid plugin entry {e!r}: {exc}") from exc
    return refs


def load_mcp_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcp.json 'mcpServers' must be an object")
    return servers

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from jean.ports import PluginRef

# First char alphanumeric (rejects a leading '-'); restricted charset; no '..'.
_SAFE_URL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:\-]*$")  # marketplace URL, ref
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")  # plugin: single path segment, no '/'


def _validate(ref: PluginRef) -> None:
    for field, value, pattern in (
        ("marketplace", ref.marketplace, _SAFE_URL),
        ("plugin", ref.plugin, _SAFE_NAME),
        ("ref", ref.ref, _SAFE_URL),
    ):
        if not pattern.match(value) or ".." in value:
            raise ValueError(f"unsafe {field} in jean.json: {value!r}")


def load_plugin_manifest(path: Path) -> list[PluginRef]:
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("jean.json must be a JSON object")
    entries = data.get("plugins", [])
    refs: list[PluginRef] = []
    for e in entries:
        try:
            pref = PluginRef(e["marketplace"], e["plugin"], e["ref"])
        except (KeyError, TypeError) as exc:
            raise ValueError(f"invalid plugin entry {e!r}: {exc}") from exc
        _validate(pref)
        refs.append(pref)
    return refs


def load_mcp_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("mcp.json must be a JSON object")
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcp.json 'mcpServers' must be an object")
    return servers

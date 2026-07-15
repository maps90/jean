from __future__ import annotations

import enum
import re
from typing import Any


class Risk(enum.Enum):
    """A tool call's risk, decided by code -- never by the model.

    SAFE  -> run without asking (routine work).
    RISKY -> ask a human (one of the four gated categories).
    DENY  -> refuse in code, never even prompt.
    """

    SAFE = "safe"
    RISKY = "risky"
    DENY = "deny"


DENY_MESSAGE = (
    "This action is disabled. OAuth connect must go through the controlled "
    "flow, not the synthesized auth tool."
)

# --- deny (never allow, never ask) ---
_DENY_MCP = re.compile(r"^mcp__.+__(authenticate|complete_authentication)$")

# --- Bash command patterns, per category. Matched case-insensitively against
#     the *verbatim* command string. This table IS the security surface: adding
#     a hole here silently widens what runs unattended. Review it as such. ---
_DESTRUCTIVE = re.compile(
    r"""
    \brm\s+-[a-z]*f          # rm -rf / -fr
    | \bgit\s+push\b.*(--force|-f)\b
    | \bgit\s+reset\s+--hard\b
    | \bgit\s+clean\s+-[a-z]*f
    | \bkubectl\s+delete\b
    | \bdrop\s+(table|database|schema)\b
    | \btruncate\b
    | \bdelete\s+from\b
    | \bmkfs\b
    | \bdd\s+if=
    | >\s*/dev/
    """,
    re.IGNORECASE | re.VERBOSE,
)
_SECRETS = re.compile(
    r"""
    (^|[\s/])\.env(\b|$)
    | \bid_rsa\b
    | \.pem\b | \.key\b
    | \bvault\b
    | \bkubectl\b.*\bsecret
    | \bcredentials?\b
    | \.ssh/
    """,
    re.IGNORECASE | re.VERBOSE,
)
_EXTERNAL = re.compile(
    r"""
    \bcurl\b | \bwget\b
    | \bgh\s+pr\s+create\b
    | \b(npm|pip|cargo|gem)\s+publish\b
    | \bgit\s+push\b
    | \bmail\b | \bsendmail\b
    | https?://
    """,
    re.IGNORECASE | re.VERBOSE,
)
_PROD_INFRA = re.compile(
    r"""
    \bkubectl\s+(apply|rollout|scale|patch|drain|cordon|edit)\b
    | \bterraform\s+(apply|destroy)\b
    | \bhelm\s+(install|upgrade|uninstall)\b
    | \b(pip|pip3)\s+install\b
    | \bnpm\s+(install|ci)\b
    | \b(apt|apt-get|yum|brew)\s+install\b
    | \bdocker\s+push\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
_BASH_RISK = (_DESTRUCTIVE, _SECRETS, _EXTERNAL, _PROD_INFRA)

# --- file paths that mean secrets, for Write/Edit ---
_SECRET_PATH = re.compile(
    r"(^|/)\.env(\.|$)|/\.ssh/|\bid_rsa\b|\.pem$|\.key$|/secrets?/|\bcredentials?\b",
    re.IGNORECASE,
)
_FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# --- MCP tool ids whose verb is a mutation worth a human ---
_MCP_RISK = re.compile(
    r"(delete|apply|rollout|scale|restart|drain|cordon|destroy|create|patch)",
    re.IGNORECASE,
)


def classify_risk(tool_name: str, tool_input: dict[str, Any]) -> Risk:
    """Deterministic risk of a tool call. Pure; reads structured args only."""
    if _DENY_MCP.match(tool_name):
        return Risk.DENY

    if tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        if any(pat.search(command) for pat in _BASH_RISK):
            return Risk.RISKY
        return Risk.SAFE

    if tool_name in _FILE_TOOLS:
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        return Risk.RISKY if _SECRET_PATH.search(path) else Risk.SAFE

    if tool_name.startswith("mcp__"):
        # jean's own Slack tools and read-only MCP calls never reach can_use_tool
        # (they are in allowed_tools), so a mutation verb here is a real action.
        return Risk.RISKY if _MCP_RISK.search(tool_name) else Risk.SAFE

    return Risk.SAFE

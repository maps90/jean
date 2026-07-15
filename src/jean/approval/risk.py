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
#     A force flag can be combined-short (-rf), separated (-r -f / -f -r), or
#     long (--force, --recursive --force) -- match a force flag in any of
#     those forms, in any order relative to other flags, without flagging a
#     plain (non-force) rm or git clean.
_FORCE_FLAG = r"(?:-[a-zA-Z]+\s+|--\w+\s+)*(?:-[a-zA-Z]*f[a-zA-Z]*\b|--force\b)"
_DESTRUCTIVE = re.compile(
    rf"""
    \brm\s+{_FORCE_FLAG}          # rm -rf / -r -f / -f -r / --force
    | \bgit\s+reset\s+--hard\b
    | \bgit\s+clean\s+{_FORCE_FLAG}
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
    | \bprintenv\b
    | \becho\b.*\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD)\w*\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
_EXTERNAL = re.compile(
    r"""
    \bcurl\b | \bwget\b
    | \bscp\b
    | \brsync\b.*(?:@[\w.-]+:|::)
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
_FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "Read"}

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
        # Only jean's own Slack tools are in allowed_tools and skip can_use_tool.
        # Every plugin MCP call -- read-only and mutating alike -- reaches here,
        # so a mutation verb in the tool id is what separates RISKY from SAFE.
        return Risk.RISKY if _MCP_RISK.search(tool_name) else Risk.SAFE

    return Risk.SAFE

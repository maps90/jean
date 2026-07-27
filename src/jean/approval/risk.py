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

# --- rm: recursion, or a dangerous target -- NOT the force flag on its own ---
#
# `\brm\s+<force flag>` used to be the whole rule, so `rm -f slide-*.jpg` read as
# destructive. That is a scratch-file cleanup the pptx skill's own workflow
# PRESCRIBES ("The `rm` clears stale images from prior runs"), so jean gated a
# command its own skill told the agent to run -- and "Always allow" could not
# absorb it, because the surrounding compound command varied by one argument
# between attempts (`tail -20` vs `tail -5`), which the CLI's narrow suggested
# pattern does not match.
#
# What actually distinguishes danger is recursion and the target, not `-f`:
# `rm -f one-file` removes what it names, while `rm -rf dir` removes a tree and
# `rm -f /etc/resolv.conf` breaks the box. So both of those still ask, and the
# force flag alone no longer does.
_RECURSIVE_FLAG = r"(?:-[a-zA-Z]+\s+|--\w+\s+)*(?:-[a-zA-Z]*r[a-zA-Z]*\b|--recursive\b)"
# An ABSOLUTE (or `~`) target is dangerous by default; the two scratch locations
# are the exceptions. Enumerating dangerous directories was the first attempt and
# it leaked -- `rm --force /data` slipped through a list of /etc, /usr, /var and
# friends. Default-deny is the only version that holds: anything absolute asks
# unless it is somewhere the agent is supposed to be scribbling.
#
#   /tmp/...        -- where the document skills stage their work
#   .../workspaces/ -- jean's own per-thread scratch dir (settings.home/workspaces)
#
# A relative target (`slide-*.jpg`, `./build/x`) never matches: it can only reach
# the agent's own working directory. Recursion is handled separately above and
# overrides both exemptions, so `rm -rf /tmp/build` still asks.
_RM_ABSOLUTE_TARGET = r"(?!\S*/workspaces/)(?:~|/(?!tmp\b))"
_DESTRUCTIVE = re.compile(
    rf"""
    \brm\s+{_RECURSIVE_FLAG}      # rm -rf / -r -f / -f -r / --recursive: a whole tree
    | \brm\s+(?:-\S+\s+|--\S+\s+)*{_RM_ABSOLUTE_TARGET}   # rm at an absolute path
    | \bgit\s+reset\s+--hard\b
    | \bgit\s+clean\s+{_FORCE_FLAG}
    | \bkubectl\s+delete\b
    | \bdrop\s+(table|database|schema)\b
    | \btruncate\b
    | \bdelete\s+from\b
    | \bmkfs\b
    | \bdd\s+if=
    # A redirect into /dev/ EXCEPT the pseudo-devices that appear in ordinary
    # commands. `>\s*/dev/` on its own matched `2>/dev/null` -- the commonest
    # shell idiom there is -- so every command that silenced stderr was
    # classified destructive and interrupted a human for an `ls`. Measured
    # against real traffic before this fix: 2 of 155 tool calls prompted, and
    # both were `ls ... 2>/dev/null`.
    #
    # Excluding rather than enumerating block devices is deliberate: an
    # unfamiliar node under /dev/ still asks, so `> /dev/some-new-thing` is
    # RISKY by default. Only the handful of targets that are harmless BY
    # DEFINITION are listed. Note this deliberately still fires on any fd
    # (`2> /dev/sdb` is a device write however you spell it).
    | >\s*/dev/(?!(?:null|zero|stdout|stderr|tty|fd/)\b)
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
    \bscp\b
    | \brsync\b.*(?:@[\w.-]+:|::)
    | \bgh\s+pr\s+create\b
    | \b(npm|pip|cargo|gem)\s+publish\b
    | \bgit\s+push\b
    | \bsendmail\b | \bmailx\b
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
# --- privilege escalation: running as another (usually root) user ---
_PRIVESC = re.compile(r"\bsudo\b|\bdoas\b", re.IGNORECASE)
_BASH_RISK = (_DESTRUCTIVE, _SECRETS, _EXTERNAL, _PROD_INFRA, _PRIVESC)

# --- file paths that mean secrets or other sensitive persistence/exec
#     vectors, for Write/Edit ---
_SECRET_PATH = re.compile(
    r"""
    (^|/)\.env(\.|$)
    | /\.ssh/
    | \bid_rsa\b
    | \.pem$ | \.key$
    | /secrets?/
    | \bcredentials?\b
    | (^|/)etc/
    | \.bashrc\b | \.bash_profile\b | \.zshrc\b | \.profile\b
    | /\.kube/config\b
    | /\.git/hooks/
    | \bauthorized_keys\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
_FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "Read"}

# --- native tools that reach outside the box: always external/outbound ---
_WEB_TOOLS = {"WebFetch", "WebSearch"}

# --- MCP tool ids whose verb is a mutation worth a human. `(?<!un)cordon`
#     matches `cordon`/`nodes_cordon` but not `uncordon`/`nodes_uncordon`. ---
_MCP_RISK = re.compile(
    r"(delete|apply|rollout|scale|restart|drain|(?<!un)cordon|destroy|create|patch"
    r"|evict|replace|remove|terminate)",
    re.IGNORECASE,
)


# --- fetch (curl/wget): judged by DESTINATION, not by the word -------------
#
# `\bcurl\b` used to sit in _EXTERNAL, so every curl asked -- including
# `curl -s -o /dev/null -w "%{http_code}" https://api.github.com`, a connectivity
# probe that discards its own output. But curl is also the one command in the
# agent's shell that can move data OUT: that shell can read JEAN_DATABASE_URL,
# the Slack bot token, the Grafana and Elasticsearch tokens, the GitHub token and
# the mounted kubernetes service-account token. The pod is disposable; those are
# not. So the rule is not "allow curl" or "gate curl" but "where is it going":
#
#   - a host the deployment has configured (JEAN_FETCH_ALLOWED_HOSTS) -> expected
#   - anything else -> a random URL, ask
#   - sending data (-d/-X POST/-T/-F) -> ask WHEREVER it goes, because a
#     configured host is not a licence to push data to it (a GitHub gist is a URL
#     anyone can read)
#   - a host jean cannot determine (`curl $TARGET`) -> ask; reading the variable
#     NAME would be trivially defeated
#
# Empty allowlist -- the default -- gates every fetch, so this cannot silently
# open anything for a deployment that has not named its hosts.
_FETCH_CMD = re.compile(r"\b(?:curl|wget)\b", re.IGNORECASE)
_FETCH_SENDS_DATA = re.compile(
    r"""
    \s-d\b | \s--data(?:-\w+)?\b        # -d / --data / --data-binary / --data-raw
    | \s-T\b | \s--upload-file\b
    | \s-F\b | \s--form\b
    | \s--post\d*\b                     # wget --post-data / --post-file
    | \s-X\s*(?:POST|PUT|PATCH|DELETE)\b
    | \s--request\s*(?:POST|PUT|PATCH|DELETE)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
_URL_RE = re.compile(r"\bhttps?://([^\s/?#'\"]+)", re.IGNORECASE)


def _fetch_is_risky(command: str, allowed_hosts: frozenset[str]) -> bool:
    """True when a curl/wget in `command` deserves a human.

    Host comparison is exact on the hostname (port stripped), never a substring:
    `api.github.com.evil.tld` and `raw.api.github.com` must not inherit
    `api.github.com`'s trust.
    """
    if not _FETCH_CMD.search(command):
        return False
    if _FETCH_SENDS_DATA.search(command):
        return True
    urls = _URL_RE.findall(command)
    if not urls:
        return True  # nothing to verify -- interpolated, or no URL at all
    return any(u.rsplit("@", 1)[-1].split(":", 1)[0].lower() not in allowed_hosts for u in urls)


def classify_risk(
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    fetch_allowed_hosts: frozenset[str] = frozenset(),
) -> Risk:
    """Deterministic risk of a tool call. Pure; reads structured args only.

    `fetch_allowed_hosts` is passed in rather than read from Settings so this stays
    a pure function -- server.py derives it once and agent_options threads it here.
    Empty (the default) means every curl/wget asks, i.e. the behaviour before the
    destination rule existed.
    """
    if _DENY_MCP.match(tool_name):
        return Risk.DENY

    if tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        if any(pat.search(command) for pat in _BASH_RISK):
            return Risk.RISKY
        if _fetch_is_risky(command, fetch_allowed_hosts):
            return Risk.RISKY
        return Risk.SAFE

    if tool_name in _FILE_TOOLS:
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        return Risk.RISKY if _SECRET_PATH.search(path) else Risk.SAFE

    if tool_name in _WEB_TOOLS:
        # Native web tools always reach outside the box -- external/outbound.
        return Risk.RISKY

    if tool_name.startswith("mcp__"):
        # Only jean's own Slack tools are in allowed_tools and skip can_use_tool.
        # Every plugin MCP call -- read-only and mutating alike -- reaches here,
        # so a mutation verb in the tool id is what separates RISKY from SAFE.
        return Risk.RISKY if _MCP_RISK.search(tool_name) else Risk.SAFE

    return Risk.SAFE

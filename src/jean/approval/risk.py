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
#   - a host jean cannot determine (`curl $TARGET`) -> ask; reading the variable
#     NAME would be trivially defeated
#
# On a configured host the HTTP METHOD decides, because the methods differ in what
# they can do to something that already exists:
#
#   - GET/HEAD/OPTIONS read, and POST creates something new (a query, a search
#     body, a webhook, an annotation) -> no click. Requiring one made ordinary
#     work -- POSTing an Elasticsearch query is a GET with a body -- unusable.
#   - PUT/PATCH/DELETE replace, edit or destroy a resource that is already there.
#     That is the same class of act as any other mutation jean asks about, and it
#     is not undone by the pod being disposable: the damage is on the far side.
#
# An unrecognised explicit method asks. There is no list of "safe" verbs to fall
# back on, and a method jean does not model is one it cannot reason about.
#
# Empty allowlist -- the default -- gates every fetch, so this cannot silently
# open anything for a deployment that has not named its hosts.
_FETCH_CMD = re.compile(r"\b(?:curl|wget)\b", re.IGNORECASE)
# Read-or-create. Anything outside this set asks, so the gap is fail-closed.
_FETCH_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "POST"})
_EXPLICIT_METHOD = re.compile(
    r"""(?:\s-X\s*|\s--request[\s=]*|\s--method[\s=]*)   # curl -X/--request, wget --method
        ([A-Za-z]+)""",
    re.IGNORECASE | re.VERBOSE,
)
# Flags that imply a method when none is given explicitly. -T/--upload-file is
# curl's PUT; the body flags are its POST.
_IMPLIES_PUT = re.compile(r"\s-T\b | \s--upload-file\b", re.IGNORECASE | re.VERBOSE)
_IMPLIES_POST = re.compile(
    r"""
    \s-d\b | \s--data(?:-\w+)?\b        # -d / --data / --data-binary / --data-raw
    | \s--json\b
    | \s-F\b | \s--form\b
    | \s--post-(?:data|file)\b          # wget
    """,
    re.IGNORECASE | re.VERBOSE,
)
_URL_RE = re.compile(r"\bhttps?://([^\s/?#'\"]+)", re.IGNORECASE)


def _fetch_methods(command: str) -> set[str]:
    """Every HTTP method this command could use, upper-cased.

    A compound command may hold several fetches (`curl A && curl -X DELETE B`),
    and the classifier judges the whole string -- so collect all of them and let
    the caller require every one to be acceptable.
    """
    explicit = {m.upper() for m in _EXPLICIT_METHOD.findall(command)}
    if explicit:
        return explicit
    if _IMPLIES_PUT.search(command):
        return {"PUT"}
    if _IMPLIES_POST.search(command):
        return {"POST"}
    return {"GET"}


def _fetch_is_risky(command: str, allowed_hosts: frozenset[str]) -> bool:
    """True when a curl/wget in `command` deserves a human.

    Host comparison is exact on the hostname (port stripped), never a substring:
    `api.github.com.evil.tld` and `raw.api.github.com` must not inherit
    `api.github.com`'s trust.
    """
    if not _FETCH_CMD.search(command):
        return False
    if not _FETCH_SAFE_METHODS.issuperset(_fetch_methods(command)):
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

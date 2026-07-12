from __future__ import annotations

import re
from dataclasses import dataclass, field

# Slack id shapes: users `U`/`W`-prefixed, channels `C`/`G`/`D`-prefixed.
USER_ID_RE = re.compile(r"^[UW][A-Z0-9]+$")
CHANNEL_ID_RE = re.compile(r"^[CGD][A-Z0-9]+$")


def _check_user_id(value: str) -> str:
    if not USER_ID_RE.match(value):
        raise ValueError(f"invalid user_id: {value!r} (must match {USER_ID_RE.pattern})")
    return value


def _check_channel_id(value: str) -> str:
    if not CHANNEL_ID_RE.match(value):
        raise ValueError(f"invalid channel id: {value!r} (must match {CHANNEL_ID_RE.pattern})")
    return value


@dataclass
class Identity:
    """Who jean is, per the persona doc."""

    name: str = "jean"
    role: str = ""


@dataclass
class Manager:
    """The Slack user jean answers to; must be a grounded, valid Slack user id."""

    user_id: str
    name: str = ""

    def __post_init__(self) -> None:
        _check_user_id(self.user_id)


@dataclass
class ApproverEntry:
    """One approver: matches `scope` keywords against an approval summary;
    `catchall=True` means it matches any summary regardless of scope."""

    user_id: str
    scope: str = ""
    catchall: bool = False

    def __post_init__(self) -> None:
        _check_user_id(self.user_id)


@dataclass
class SoulData:
    """Typed projection of IDENTITY.md. Every Slack id here must have been
    verified present verbatim in the raw persona text (see persona/extract.py
    `assert_ids_grounded`) before any gate uses it -- the trust boundary."""

    identity: Identity
    manager: Manager | None
    allowed_channels: list[str] = field(default_factory=list)
    dm_allowed_users: list[str] = field(default_factory=list)
    blocked_users: list[str] = field(default_factory=list)
    approvers: list[ApproverEntry] = field(default_factory=list)
    mandate: str = ""
    values: list[str] = field(default_factory=list)
    approval_timeout_seconds: int = 0

    def __post_init__(self) -> None:
        for ch in self.allowed_channels:
            _check_channel_id(ch)
        for uid in (*self.dm_allowed_users, *self.blocked_users):
            _check_user_id(uid)


EXTRACTION_PROMPT = """\
You are extracting a structured "soul" from a persona document (IDENTITY.md)
for a Slack-native AI teammate. The document names the teammate; do not assume
a name. Read the raw text below and return ONLY a JSON object (no prose, no
markdown fences) with this shape:

{
  "identity": {"name": "<the teammate's name, exactly as the document gives it>",
               "role": "<one-line role>"},
  "manager": {"user_id": "<Slack user id, e.g. U0123ABCD>", "name": "<display name>"},
  "allowed_channels": ["<Slack channel id>", ...],
  "dm_allowed_users": ["<Slack user id>", ...],
  "blocked_users": ["<Slack user id>", ...],
  "approvers": [{"user_id": "<Slack user id>", "scope": "<keywords>", "catchall": false}, ...],
  "mandate": "<what the teammate is for, one paragraph>",
  "values": ["<value>", ...],
  "approval_timeout_seconds": 0
}

CRITICAL RULE: every Slack id (user or channel) you output MUST appear
verbatim, character-for-character, somewhere in the raw text below. Never
invent, guess, or normalize an id. If you cannot find a real id for a field,
omit that entry rather than making one up -- a downstream safety check
rejects any id it cannot find in the source text.

Raw persona text follows:
---
"""

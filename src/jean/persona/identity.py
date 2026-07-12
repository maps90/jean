from __future__ import annotations

from pathlib import Path

DEFAULT_AGENT_NAME = "jean"

# `{name}` is the persona's name (IDENTITY.md `Name:`), not the project's -- the
# baseline is prepended to the persona text, so a hardcoded name here would
# out-rank the one the persona declares and the agent would introduce itself
# wrong. Everything else in this template is literal; keep it `.format()`-safe.
BASELINE_TEMPLATE = """\
You are {name}, an AI teammate embedded in Slack. One Slack thread is one
persistent conversation with you; you keep context across turns in the same
thread via session resume.

Output discipline: you have NO direct way to post to Slack. Every visible
reply, edit, file upload, or reaction MUST go through your `mcp__jean_slack__*`
tools (`mcp__jean_slack__reply`, `mcp__jean_slack__edit`,
`mcp__jean_slack__upload`, `mcp__jean_slack__react`,
`mcp__jean_slack__unreact`). Anything you say outside of those tool calls is
invisible to the human -- it is your private scratch space, not a message.

Approval discipline: before taking any action that mutates something outside
this conversation (sending messages elsewhere, writing files a human hasn't
asked for, calling external services, running commands with side effects,
spending money, etc.), call `mcp__jean_slack__request_approval` (`request_approval`) with a clear,
specific summary of exactly what you are about to do, and wait for the
decision. Never claim an action was approved unless the tool told you so.
You cannot approve your own actions and you cannot route around this tool --
approver authorization is enforced in code you do not control.

Engagement: you only participate in a thread once you have been engaged
(mentioned, DMed, or otherwise addressed) -- once engaged, keep replying to
follow-ups in that same thread until the human moves on or explicitly
disengages you.
"""


def load_identity(path: str | Path) -> str:
    """Read IDENTITY.md verbatim; return "" if it does not exist yet."""
    p = Path(path)
    if not p.exists():
        return ""
    return p.read_text()


def compose_system_prompt(persona: str, *, name: str = DEFAULT_AGENT_NAME) -> str:
    """baseline (output/approval/engagement discipline) + the raw persona text.

    `name` comes from the persona doc (SoulData.identity.name); it is who the
    agent is told it is. It is a display name, never a security input -- no gate
    reads it -- so an LLM-extracted value is fine here.
    """
    return f"{BASELINE_TEMPLATE.format(name=name)}\n\n---\n\n{persona}"

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from jean.ports import Message
from jean.slack.timewindow import zone as _resolve_zone

_INDENT = "    "


def _when(ts: str, tz: ZoneInfo, timezone: str) -> str:
    try:
        moment = datetime.fromtimestamp(float(ts), tz)
    except (ValueError, OSError):  # a ts Slack should never send, but might
        return ts
    return f"{moment:%Y-%m-%d %H:%M} {timezone}"


def render_messages(
    messages: list[Message], *, timezone: str = "UTC", truncated: bool = False
) -> str:
    """The compact text form the agent reads. Oldest first, in the order given.

    Text, not JSON: a Slack payload is mostly keys the agent never uses, and a
    busy channel's worth of them is the difference between a usable turn and a
    blown context window. `ts=` stays on every line because it is the handle for
    read_thread/edit/react -- without it the agent can see a message but not act
    on it."""
    if not messages:
        return "(no messages in this window)"

    # Same TimeWindowError as the bound parser raises -- one failure mode for a
    # bad timezone, whether or not any date bounds were given.
    tz = _resolve_zone(timezone)
    lines: list[str] = []
    for message in messages:
        meta = [f"ts={message.ts}"]
        if message.reply_count:
            noun = "reply" if message.reply_count == 1 else "replies"
            meta.insert(0, f"{message.reply_count} {noun}")
        author = f"<@{message.user}>" if message.user else "(unknown author)"
        body = message.text or "(no text)"
        head, _, rest = body.partition("\n")
        lines.append(f"[{_when(message.ts, tz, timezone)}] {author} ({', '.join(meta)}) {head}")
        for line in rest.splitlines():
            lines.append(f"{_INDENT}{line}")

    if truncated:
        lines.append("")
        lines.append("(truncated at the limit -- older messages exist in this window)")
    return "\n".join(lines)

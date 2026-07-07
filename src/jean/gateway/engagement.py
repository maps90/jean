from __future__ import annotations

import re
from dataclasses import dataclass

from jean.persona.model import SoulData

_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]+)?>")


def mentions_in(text: str) -> list[str]:
    """Every Slack user id `<@Uxxxx>`-mentioned in `text`, in order."""
    return _MENTION_RE.findall(text)


@dataclass
class Decision:
    handle: bool
    engage: bool | None  # None = leave the thread's engagement flag unchanged


def decide(
    *,
    bot_id: str,
    channel: str,
    thread_ts: str,
    text: str,
    is_dm: bool,
    soul: SoulData,
    engaged: bool,
    author_id: str | None = None,
) -> Decision:
    """Pure engagement/authorization decision -- no I/O, no gates. `engaged`
    is read from the SessionStore by the caller (gateway/app.py) so this stays
    synchronous and trivially testable. channel/thread_ts are accepted for
    future channel-scoping (e.g. allowed_channels) but unused today.
    """
    del channel, thread_ts  # reserved for future channel-scoping

    if author_id is not None and author_id in soul.blocked_users:
        return Decision(handle=False, engage=None)

    if is_dm:
        return Decision(handle=True, engage=True)

    mentions = mentions_in(text)
    if bot_id in mentions:
        return Decision(handle=True, engage=True)

    if mentions:
        # Someone else was addressed in this thread -- jean steps back.
        return Decision(handle=False, engage=False)

    if engaged:
        return Decision(handle=True, engage=None)

    return Decision(handle=False, engage=None)

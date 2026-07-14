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
    partner: str | None  # the thread's partner AFTER this message


def decide(
    *,
    bot_id: str,
    channel: str,
    thread_ts: str,
    text: str,
    is_dm: bool,
    soul: SoulData,
    partner: str | None,
    author_id: str | None = None,
) -> Decision:
    """Pure engagement/authorization decision -- no I/O, no gates. `partner` is the
    thread's current conversation partner, read from the SessionStore by the caller
    (gateway/app.py) so this stays synchronous and trivially testable.

    `Decision.partner` is always the *resulting* partner, never a "leave it alone"
    sentinel: the unchanged cases just hand `partner` back. The caller compares it
    with what it read and writes only on a change -- that's what keeps an ignored
    message free of a database write.

    channel/thread_ts are accepted for future channel-scoping (e.g. allowed_channels)
    but unused today.
    """
    del channel, thread_ts  # reserved for future channel-scoping

    if author_id is not None and author_id in soul.blocked_users:
        return Decision(handle=False, partner=partner)

    if is_dm:
        return Decision(handle=True, partner=author_id)

    mentions = mentions_in(text)
    if bot_id in mentions:
        # Most recent mention wins: whoever addresses her is who she's talking to.
        # An unknown author leaves no partner, so the thread falls back to strict
        # mention-only rather than to a stale or wrong one.
        return Decision(handle=True, partner=author_id)

    if mentions:
        # Someone else was addressed in this thread -- jean steps back.
        return Decision(handle=False, partner=None)

    if author_id is not None and author_id == partner:
        # The partner's plain follow-up: no re-@mention needed.
        return Decision(handle=True, partner=partner)

    # Anyone else's plain message. This is the line the whole feature exists for:
    # no turn, no tokens, and no place in the thread's lock queue.
    return Decision(handle=False, partner=partner)

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
    # Which branch decided, for the log. Engagement is the one place where doing
    # nothing is a legitimate outcome, so "no reply" is ambiguous by design --
    # this is what makes it greppable instead of a mystery.
    reason: str = ""


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

    Channel scoping and the DM allowlist are enforced here, ahead of the
    mention/partner rules: soul.md tells readers that outside its listed channels,
    and in DMs, only the manager engages -- and until this existed that sentence was
    decoration, because this function deleted its `channel` argument.
    """
    del thread_ts  # reserved for future per-thread scoping

    # First and unconditional: being blocked overrides being the manager.
    if author_id is not None and author_id in soul.blocked_users:
        return Decision(handle=False, partner=partner, reason="blocked-user")

    is_manager = (
        author_id is not None and soul.manager is not None and author_id == soul.manager.user_id
    )

    if is_dm:
        # DMs are deliberately NOT gated here. soul.md's "## DM allowlist" section
        # says only the manager engages in DMs by default, and `dm_allowed_users`
        # exists in SoulData for exactly that -- but README documents the opposite
        # ("DM it directly -- either makes you jean's conversation partner"), and
        # gating them would change product behaviour for every deployment rather
        # than close a doc/code gap. Left as its own decision; see the PR.
        #
        # An unattributable event must not mutate the partner: fall back to the
        # existing one rather than wiping it with `None`.
        return Decision(
            handle=True, partner=author_id if author_id is not None else partner, reason="dm"
        )

    # An EMPTY list means unrestricted, not "nothing is allowed". The two are
    # indistinguishable here -- a soul.md that never had the section extracts the
    # same empty list as one that lists nothing -- and failing closed would silence
    # jean in every channel the moment this shipped. server.py logs at boot when the
    # list is empty, so an operator can see the control is inert rather than assume
    # it is protecting them.
    if soul.allowed_channels and channel not in soul.allowed_channels and not is_manager:
        # Partner deliberately handed back unchanged: an ignored message must cost
        # nothing, including a database write, and a bystander in a channel jean was
        # never invited into must not be able to clear whoever it was talking to.
        return Decision(handle=False, partner=partner, reason="channel-not-allowed")

    mentions = mentions_in(text)
    if bot_id in mentions:
        # Most recent mention wins: whoever addresses her is who she's talking to.
        # An unattributable author leaves the existing partner alone rather than
        # wiping it -- an anonymous or bot-authored event must not clear it.
        return Decision(
            handle=True, partner=author_id if author_id is not None else partner, reason="mention"
        )

    if mentions:
        # Someone else was addressed. Only the current partner can disengage her
        # this way (the handoff: "@budi can you take this?") -- a bystander
        # mentioning anyone else is as inert as their plain chatter and must not
        # touch the partner.
        if author_id is not None and author_id == partner:
            return Decision(handle=False, partner=None, reason="handoff")
        return Decision(handle=False, partner=partner, reason="other-mentioned")

    if author_id is not None and author_id == partner:
        # The partner's plain follow-up: no re-@mention needed.
        return Decision(handle=True, partner=partner, reason="partner-followup")

    # Anyone else's plain message. This is the line the whole feature exists for:
    # no turn, no tokens, and no place in the thread's lock queue.
    return Decision(handle=False, partner=partner, reason="not-addressed")

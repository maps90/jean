from __future__ import annotations

from jean.gateway.engagement import decide
from jean.persona.model import Identity, Manager, SoulData

MANAGER = "UMANAGER"
OTHER = "UOTHER"
ALLOWED = "C0ALLOWED"
UNLISTED = "C0UNLISTED"


def _soul(**kw) -> SoulData:
    base = dict(identity=Identity(name="Anya"), manager=Manager(user_id=MANAGER))
    base.update(kw)
    return SoulData(**base)


def _decide(**kw):
    args = dict(
        bot_id="UBOT",
        channel=ALLOWED,
        thread_ts="1.0",
        text="hey <@UBOT>",
        is_dm=False,
        soul=_soul(allowed_channels=[ALLOWED]),
        partner=None,
        author_id=OTHER,
    )
    args.update(kw)
    return decide(**args)


# --- channels on the list behave exactly as before --------------------------


def test_a_listed_channel_is_unchanged():
    assert _decide().handle is True
    assert _decide().reason == "mention"


def test_a_listed_channel_still_honours_the_partner_follow_up():
    d = _decide(text="plain follow-up", partner=OTHER)
    assert d.handle is True and d.reason == "partner-followup"


# --- channels NOT on the list -----------------------------------------------


def test_an_unlisted_channel_ignores_everyone_but_the_manager():
    """soul.md promises: "In any channel NOT listed here ... only the manager or
    backup manager engages." Until now that sentence was decoration --
    engagement.decide() deleted the channel argument."""
    d = _decide(channel=UNLISTED, author_id=OTHER)
    assert d.handle is False
    assert d.reason == "channel-not-allowed"


def test_the_manager_is_heard_in_any_channel():
    d = _decide(channel=UNLISTED, author_id=MANAGER)
    assert d.handle is True and d.reason == "mention"


def test_an_unlisted_channel_does_not_wipe_an_existing_partner():
    """A dropped message must cost nothing -- including a database write."""
    d = _decide(channel=UNLISTED, author_id=OTHER, partner=MANAGER)
    assert d.handle is False and d.partner == MANAGER


def test_an_unattributable_author_is_not_treated_as_the_manager():
    """`author_id=None` (an anonymous or bot-authored event) must not pass the
    manager check -- that would make every unlisted channel open to anything jean
    cannot attribute."""
    d = _decide(channel=UNLISTED, author_id=None)
    assert d.handle is False and d.reason == "channel-not-allowed"


# --- the empty-list decision (documented, deliberate) -----------------------


def test_no_configured_channels_means_no_restriction():
    """An empty list is indistinguishable from "the section was never written", and
    failing closed there would silence jean in every channel on upgrade. Fails OPEN
    on purpose; server.py logs at boot when the control is inert."""
    d = _decide(channel=UNLISTED, author_id=OTHER, soul=_soul(allowed_channels=[]))
    assert d.handle is True and d.reason == "mention"


# --- DMs are deliberately out of scope here ------------------------------------


def test_dms_are_not_gated_by_this_change():
    """soul.md's "## DM allowlist" says only the manager engages in DMs, and
    `dm_allowed_users` exists in SoulData for it -- but README documents the
    opposite, so gating DMs changes product behaviour rather than closing a
    doc/code gap. Pinned here so a future change to it is deliberate."""
    assert _decide(is_dm=True, channel="D0X", author_id=OTHER).handle is True


def test_channel_scoping_does_not_leak_into_dms():
    """A DM's "channel" is a D-id that can never be in allowed_channels; the channel
    gate must not accidentally silence every DM."""
    d = _decide(is_dm=True, channel="D0X", author_id=OTHER, soul=_soul(allowed_channels=[ALLOWED]))
    assert d.handle is True and d.reason == "dm"


# --- ordering ---------------------------------------------------------------


def test_a_blocked_manager_is_still_blocked():
    """The blocked check must stay first: it is the only rule that overrides being
    the manager."""
    d = _decide(
        channel=UNLISTED,
        author_id=MANAGER,
        soul=_soul(allowed_channels=[], blocked_users=[MANAGER]),
    )
    assert d.handle is False and d.reason == "blocked-user"


def test_channel_scoping_precedes_the_handoff_rule():
    """A bystander in an unlisted channel must not be able to disengage jean by
    mentioning someone else -- the channel gate decides first."""
    d = _decide(channel=UNLISTED, author_id=OTHER, text="hey <@USOMEONE>", partner=OTHER)
    assert d.handle is False and d.reason == "channel-not-allowed"
    assert d.partner == OTHER, "an unlisted channel must not clear the partner"

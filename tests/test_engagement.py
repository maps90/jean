from __future__ import annotations

from jean.gateway.engagement import decide, mentions_in
from jean.persona.model import Identity, Manager, SoulData


def _soul(**kwargs) -> SoulData:
    defaults = dict(identity=Identity(name="jean"), manager=Manager(user_id="U00001"))
    defaults.update(kwargs)
    return SoulData(**defaults)


def test_mentions_in_extracts_all_ids():
    assert mentions_in("hey <@U11111> and <@W22222>, look at this") == ["U11111", "W22222"]


def test_mentions_in_empty_when_none():
    assert mentions_in("no mentions here") == []


def test_mentions_in_extracts_piped_mention_form():
    # Slack renders a mention as `<@U123|display-name>` in some contexts.
    assert mentions_in("hey <@U11111|alice>, look at this") == ["U11111"]


def test_blocked_author_is_dropped():
    soul = _soul(blocked_users=["U66666"])
    d = decide(
        bot_id="UBOT",
        channel="C1",
        thread_ts="1.0",
        text="hello",
        is_dm=False,
        soul=soul,
        partner="U66666",
        author_id="U66666",
    )
    assert d.handle is False
    assert d.partner == "U66666"  # unchanged -- blocking is not disengaging


def test_dm_always_handles_and_takes_the_author_as_partner():
    soul = _soul()
    d = decide(
        bot_id="UBOT",
        channel="D1",
        thread_ts="1.0",
        text="hello",
        is_dm=True,
        soul=soul,
        partner=None,
        author_id="U11111",
    )
    assert d.handle is True
    assert d.partner == "U11111"


def test_mention_of_bot_handles_and_makes_the_mentioner_the_partner():
    soul = _soul()
    d = decide(
        bot_id="UBOT",
        channel="C1",
        thread_ts="1.0",
        text="hey <@UBOT> help me",
        is_dm=False,
        soul=soul,
        partner=None,
        author_id="U11111",
    )
    assert d.handle is True
    assert d.partner == "U11111"


def test_mention_by_a_second_person_takes_over_the_conversation():
    soul = _soul()
    d = decide(
        bot_id="UBOT",
        channel="C1",
        thread_ts="1.0",
        text="<@UBOT> actually, over here",
        is_dm=False,
        soul=soul,
        partner="U11111",
        author_id="U22222",
    )
    assert d.handle is True
    assert d.partner == "U22222"


def test_mention_of_someone_else_clears_the_partner_and_does_not_handle():
    soul = _soul()
    d = decide(
        bot_id="UBOT",
        channel="C1",
        thread_ts="1.0",
        text="hey <@U22222> can you help",
        is_dm=False,
        soul=soul,
        partner="U11111",
        author_id="U11111",
    )
    assert d.handle is False
    assert d.partner is None


def test_plain_follow_up_from_the_partner_is_handled():
    soul = _soul()
    d = decide(
        bot_id="UBOT",
        channel="C1",
        thread_ts="1.0",
        text="ok now restart it",
        is_dm=False,
        soul=soul,
        partner="U11111",
        author_id="U11111",
    )
    assert d.handle is True
    assert d.partner == "U11111"


def test_plain_message_from_a_non_partner_is_ignored():
    """The whole point of the feature: Budi's aside to Dimas costs no turn."""
    soul = _soul()
    d = decide(
        bot_id="UBOT",
        channel="C1",
        thread_ts="1.0",
        text="that started after friday's deploy",
        is_dm=False,
        soul=soul,
        partner="U11111",
        author_id="U22222",
    )
    assert d.handle is False
    assert d.partner == "U11111"  # unchanged: Dimas is still the partner


def test_plain_message_with_no_partner_is_ignored():
    soul = _soul()
    d = decide(
        bot_id="UBOT",
        channel="C1",
        thread_ts="1.0",
        text="random chatter",
        is_dm=False,
        soul=soul,
        partner=None,
        author_id="U11111",
    )
    assert d.handle is False
    assert d.partner is None


def test_mention_of_bot_takes_priority_over_mention_of_others():
    soul = _soul()
    d = decide(
        bot_id="UBOT",
        channel="C1",
        thread_ts="1.0",
        text="hey <@UBOT> and <@U22222>",
        is_dm=False,
        soul=soul,
        partner=None,
        author_id="U11111",
    )
    assert d.handle is True
    assert d.partner == "U11111"


def test_mention_with_unknown_author_stores_no_partner():
    """Slack gave us no author id -- handle the mention, but store no partner, so
    the thread falls back to strict mention-only rather than to a wrong partner."""
    soul = _soul()
    d = decide(
        bot_id="UBOT",
        channel="C1",
        thread_ts="1.0",
        text="<@UBOT> hi",
        is_dm=False,
        soul=soul,
        partner="U11111",
        author_id=None,
    )
    assert d.handle is True
    assert d.partner is None

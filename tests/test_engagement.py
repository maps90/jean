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
        engaged=True,
        author_id="U66666",
    )
    assert d.handle is False
    assert d.engage is None


def test_dm_always_handles_and_engages():
    soul = _soul()
    d = decide(
        bot_id="UBOT",
        channel="D1",
        thread_ts="1.0",
        text="hello",
        is_dm=True,
        soul=soul,
        engaged=False,
        author_id="U11111",
    )
    assert d.handle is True
    assert d.engage is True


def test_mention_of_bot_handles_and_engages():
    soul = _soul()
    d = decide(
        bot_id="UBOT",
        channel="C1",
        thread_ts="1.0",
        text="hey <@UBOT> help me",
        is_dm=False,
        soul=soul,
        engaged=False,
        author_id="U11111",
    )
    assert d.handle is True
    assert d.engage is True


def test_mention_of_someone_else_disengages_and_does_not_handle():
    soul = _soul()
    d = decide(
        bot_id="UBOT",
        channel="C1",
        thread_ts="1.0",
        text="hey <@U22222> can you help",
        is_dm=False,
        soul=soul,
        engaged=True,
        author_id="U11111",
    )
    assert d.handle is False
    assert d.engage is False


def test_plain_reply_while_engaged_handles():
    soul = _soul()
    d = decide(
        bot_id="UBOT",
        channel="C1",
        thread_ts="1.0",
        text="continuing the conversation",
        is_dm=False,
        soul=soul,
        engaged=True,
        author_id="U11111",
    )
    assert d.handle is True
    assert d.engage is None


def test_plain_reply_while_not_engaged_is_ignored():
    soul = _soul()
    d = decide(
        bot_id="UBOT",
        channel="C1",
        thread_ts="1.0",
        text="random chatter",
        is_dm=False,
        soul=soul,
        engaged=False,
        author_id="U11111",
    )
    assert d.handle is False
    assert d.engage is None


def test_mention_of_bot_takes_priority_over_mention_of_others():
    soul = _soul()
    d = decide(
        bot_id="UBOT",
        channel="C1",
        thread_ts="1.0",
        text="hey <@UBOT> and <@U22222>",
        is_dm=False,
        soul=soul,
        engaged=False,
        author_id="U11111",
    )
    assert d.handle is True
    assert d.engage is True

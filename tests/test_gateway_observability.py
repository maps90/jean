from __future__ import annotations

import logging

from jean.db.memory import MemoryStore
from jean.gateway.app import Gateway
from jean.gateway.engagement import decide
from jean.persona.model import Identity, Manager, SoulData


class FakeManager:
    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    async def handle(self, channel: str, thread_ts: str, text: str) -> None:
        self.calls.append((channel, thread_ts, text))


class FakeGate:
    async def handle_action(self, action_id: str, user_id: str) -> str:
        return "approved"


class FakeChat:
    def __init__(self, fail: bool = False):
        self.reactions: list[tuple[str, str, str]] = []
        self.fail = fail

    async def react(self, channel: str, ts: str, emoji: str) -> None:
        if self.fail:
            raise RuntimeError("channel_not_found")
        self.reactions.append((channel, ts, emoji))


def _soul(**kwargs) -> SoulData:
    defaults = dict(identity=Identity(name="jean"), manager=Manager(user_id="U00001"))
    defaults.update(kwargs)
    return SoulData(**defaults)


_DEFAULT = object()


def _gateway(*, soul=None, chat=_DEFAULT, bot_id="UBOT"):
    store = MemoryStore()
    manager = FakeManager()
    # Sentinel, not `None`: `chat=None` must actually reach the Gateway as None,
    # or the no-surface test silently exercises the happy path instead.
    chat = FakeChat() if chat is _DEFAULT else chat
    gw = Gateway(
        store=store,
        manager=manager,
        gate=FakeGate(),
        bot_id=bot_id,
        soul_provider=lambda: soul or _soul(),
        chat=chat,
    )
    return gw, store, manager, chat


# --- the decision explains itself ------------------------------------------


def _reason(**kwargs) -> str:
    base = dict(
        bot_id="UBOT",
        channel="C1",
        thread_ts="1.0",
        text="hello",
        is_dm=False,
        soul=_soul(),
        partner=None,
        author_id="U11111",
    )
    base.update(kwargs)
    return decide(**base).reason


def test_every_outcome_names_its_reason():
    assert _reason(is_dm=True) == "dm"
    assert _reason(text="hey <@UBOT>") == "mention"
    assert _reason(soul=_soul(blocked_users=["U11111"])) == "blocked-user"
    assert _reason(text="hey <@UOTHER>") == "other-mentioned"
    assert _reason(text="hey <@UOTHER>", partner="U11111") == "handoff"
    assert _reason(partner="U11111") == "partner-followup"
    assert _reason(partner="U99999") == "not-addressed"


# --- the ignored message leaves a trace ------------------------------------


async def test_ignored_message_is_logged_with_its_reason(caplog):
    """The whole point: 'nine minutes of silence' must be greppable. Before
    this, an ignored message produced no log line and no Slack reaction, so it
    was indistinguishable from an event that never arrived."""
    gw, _store, manager, _chat = _gateway()

    with caplog.at_level(logging.INFO, logger="jean.gateway"):
        await gw.on_message("C1", "111.0", "just chatting", "U11111", False, message_ts="111.5")

    assert manager.calls == []  # genuinely ignored
    line = " ".join(r.getMessage() for r in caplog.records)
    assert "handle=False" in line, line
    assert "not-addressed" in line, line
    assert "C1" in line and "111.0" in line and "U11111" in line, line


async def test_handled_message_is_logged_too(caplog):
    gw, _store, manager, _chat = _gateway()

    with caplog.at_level(logging.INFO, logger="jean.gateway"):
        await gw.on_mention(
            channel="C1",
            thread_ts="111.0",
            text="hey <@UBOT>",
            author_id="U11111",
            message_ts="111.5",
        )

    assert len(manager.calls) == 1
    line = " ".join(r.getMessage() for r in caplog.records)
    assert "handle=True" in line and "mention" in line, line


async def test_wrong_handle_is_diagnosable_from_the_log(caplog):
    """A real incident: the deployment is 'anya' but the bot is @jean, so
    '@anya ...' was dropped in silence. The log must say why."""
    gw, _store, manager, _chat = _gateway()

    with caplog.at_level(logging.INFO, logger="jean.gateway"):
        await gw.on_message(
            "C1", "111.0", "<@UANYA> draft a postmortem", "U11111", False, message_ts="111.5"
        )

    assert manager.calls == []
    line = " ".join(r.getMessage() for r in caplog.records)
    assert "other-mentioned" in line, line


# --- the accepted message is visibly acknowledged --------------------------


async def test_accepted_message_gets_an_eyes_reaction_on_that_message():
    """Reacting to `message_ts`, not `thread_ts`: a follow-up deep in a thread
    must be acknowledged on itself, not on the thread's opening message."""
    gw, _store, _manager, chat = _gateway()

    await gw.on_mention(
        channel="C1",
        thread_ts="111.0",
        text="hey <@UBOT>",
        author_id="U11111",
        message_ts="222.0",
    )

    assert chat.reactions == [("C1", "222.0", "eyes")]


async def test_ignored_message_gets_no_reaction():
    gw, _store, _manager, chat = _gateway()

    await gw.on_message("C1", "111.0", "just chatting", "U11111", False, message_ts="222.0")

    assert chat.reactions == []


async def test_a_failed_reaction_does_not_lose_the_turn():
    """The ack is a nicety -- a missing scope or a deleted message must not
    cost the user their answer."""
    gw, _store, manager, _chat = _gateway(chat=FakeChat(fail=True))

    await gw.on_mention(
        channel="C1",
        thread_ts="111.0",
        text="hey <@UBOT>",
        author_id="U11111",
        message_ts="222.0",
    )

    assert len(manager.calls) == 1


async def test_gateway_works_without_a_chat_surface():
    """Single-process/test wiring may not pass one; the turn still runs."""
    gw, _store, manager, _chat = _gateway(chat=None)

    await gw.on_mention(
        channel="C1",
        thread_ts="111.0",
        text="hey <@UBOT>",
        author_id="U11111",
        message_ts="222.0",
    )

    assert len(manager.calls) == 1

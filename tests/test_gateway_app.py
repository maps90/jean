from __future__ import annotations

from jean.db.memory import MemoryStore
from jean.gateway.app import Gateway, ephemeral_for
from jean.persona.model import Identity, Manager, SoulData


class FakeManager:
    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    async def handle(self, channel: str, thread_ts: str, text: str) -> None:
        self.calls.append((channel, thread_ts, text))


class FakeGate:
    def __init__(self, result: str = "approved"):
        self.result = result
        self.calls: list[tuple[str, str]] = []

    async def handle_action(self, action_id: str, user_id: str) -> str:
        self.calls.append((action_id, user_id))
        return self.result


def _soul(**kwargs) -> SoulData:
    defaults = dict(identity=Identity(name="jean"), manager=Manager(user_id="U00001"))
    defaults.update(kwargs)
    return SoulData(**defaults)


def _gateway(store=None, manager=None, gate=None, soul=None, bot_id="UBOT"):
    store = store or MemoryStore()
    manager = manager or FakeManager()
    gate = gate or FakeGate()
    soul = soul or _soul()
    gw = Gateway(store=store, manager=manager, gate=gate, bot_id=bot_id, soul_provider=lambda: soul)
    return gw, store, manager, gate


async def test_on_mention_sets_partner_and_dispatches():
    gw, store, manager, _gate = _gateway()

    await gw.on_mention(
        channel="C1", thread_ts="111.0", text="hey <@UBOT> help me", author_id="U11111"
    )

    assert await store.get_partner("C1", "111.0") == "U11111"
    assert manager.calls == [("C1", "111.0", "hey <@UBOT> help me")]


async def test_on_mention_blocked_author_is_ignored():
    gw, store, manager, _gate = _gateway(soul=_soul(blocked_users=["U66666"]))

    await gw.on_mention(
        channel="C1", thread_ts="111.0", text="hey <@UBOT> help me", author_id="U66666"
    )

    assert await store.get_partner("C1", "111.0") is None
    assert manager.calls == []


async def test_on_message_skips_bot_mention_to_avoid_double_dispatch():
    """Slack delivers a channel @mention as BOTH app_mention and message
    events; on_mention owns bot-mentions, so on_message must no-op here."""
    gw, store, manager, _gate = _gateway()

    await gw.on_message("C1", "111.0", "hey <@UBOT> help me", "U11111", False)

    assert manager.calls == []
    assert await store.get_partner("C1", "111.0") is None


async def test_partner_follow_up_handled_but_a_bystander_is_ignored():
    """The end-to-end shape of the feature: after Dimas mentions her, his plain
    follow-ups run and Budi's asides do not."""
    gw, store, manager, _gate = _gateway()

    await gw.on_message("C1", "111.0", "just chatting", "U11111", False)
    assert manager.calls == []  # nobody has addressed her yet

    await gw.on_mention(channel="C1", thread_ts="111.0", text="hey <@UBOT>", author_id="U11111")
    manager.calls.clear()

    await gw.on_message("C1", "111.0", "budi's aside", "U22222", False)
    assert manager.calls == []  # not the partner -> no turn

    await gw.on_message("C1", "111.0", "follow-up message", "U11111", False)
    assert manager.calls == [("C1", "111.0", "follow-up message")]
    assert await store.get_partner("C1", "111.0") == "U11111"


async def test_mention_by_a_second_person_takes_over_the_conversation():
    gw, store, manager, _gate = _gateway()
    await store.set_partner("C1", "111.0", "U11111")

    await gw.on_mention(
        channel="C1", thread_ts="111.0", text="<@UBOT> over here", author_id="U22222"
    )

    assert await store.get_partner("C1", "111.0") == "U22222"
    # And now the old partner's plain message is the one that gets ignored.
    manager.calls.clear()
    await gw.on_message("C1", "111.0", "wait, what about my thing", "U11111", False)
    assert manager.calls == []


async def test_dm_message_always_handled_and_sets_partner():
    gw, store, manager, _gate = _gateway()

    await gw.on_message("D1", "111.0", "hi jean", "U11111", True)

    assert await store.get_partner("D1", "111.0") == "U11111"
    assert manager.calls == [("D1", "111.0", "hi jean")]


async def test_mention_of_someone_else_clears_the_partner():
    """The handoff: the partner herself says "@budi can you take this?"."""
    gw, store, manager, _gate = _gateway()
    await store.set_partner("C1", "111.0", "U11111")

    await gw.on_message("C1", "111.0", "hey <@U22222>", "U11111", False)

    assert await store.get_partner("C1", "111.0") is None
    assert manager.calls == []


async def test_bystander_mentioning_someone_else_does_not_clear_the_partner():
    """A bystander's mention of a third party must not disengage jean from the
    real partner's conversation -- and the partner's next plain follow-up must
    still be handled."""
    gw, store, manager, _gate = _gateway()
    await store.set_partner("C1", "111.0", "U11111")

    await gw.on_message("C1", "111.0", "hey <@U33333> can you look at this", "U22222", False)

    assert await store.get_partner("C1", "111.0") == "U11111"
    assert manager.calls == []

    await gw.on_message("C1", "111.0", "follow-up message", "U11111", False)
    assert manager.calls == [("C1", "111.0", "follow-up message")]


async def test_on_mention_with_unknown_author_leaves_the_partner_unchanged():
    """An unattributable mention must not wipe an existing partner -- it just
    handles the turn."""
    gw, store, manager, _gate = _gateway()
    await store.set_partner("C1", "111.0", "U11111")

    await gw.on_mention(channel="C1", thread_ts="111.0", text="hey <@UBOT>", author_id=None)

    assert await store.get_partner("C1", "111.0") == "U11111"
    assert manager.calls == [("C1", "111.0", "hey <@UBOT>")]


async def test_blocked_author_is_ignored():
    """A blocked user posting must not hijack the thread: the real partner
    (U11111) stays the partner, even though the blocked author (U66666) is
    someone else entirely."""
    gw, store, manager, _gate = _gateway(soul=_soul(blocked_users=["U66666"]))
    await store.set_partner("C1", "111.0", "U11111")

    await gw.on_message("C1", "111.0", "anything", "U66666", False)

    assert manager.calls == []
    assert await store.get_partner("C1", "111.0") == "U11111"


async def test_ignored_message_does_not_write_to_the_store():
    """An ignored message must cost nothing -- no turn AND no write. If this
    regresses, every bystander's message silently starts hitting the database."""
    gw, store, manager, _gate = _gateway()
    await store.set_partner("C1", "111.0", "U11111")
    writes: list[str | None] = []
    original = store.set_partner

    async def spy(channel, thread_ts, user_id):
        writes.append(user_id)
        await original(channel, thread_ts, user_id)

    store.set_partner = spy

    await gw.on_message("C1", "111.0", "budi's aside", "U22222", False)

    assert manager.calls == []
    assert writes == []


async def test_on_action_delegates_to_gate():
    gw, _store, _manager, gate = _gateway(gate=FakeGate(result="denied"))

    result = await gw.on_action("jean_appr:deny:abc", "U11111")

    assert result == "denied"
    assert gate.calls == [("jean_appr:deny:abc", "U11111")]


def test_ephemeral_only_for_clicks_that_did_nothing():
    """A click that changed nothing must say so -- silence is what made people
    click Approve over and over."""
    assert "not authorized" in (ephemeral_for("unauthorized") or "")
    assert "already decided" in (ephemeral_for("gone") or "")
    # A click that DID decide needs no ephemeral: the message rewrite is the feedback.
    assert ephemeral_for("approved") is None
    assert ephemeral_for("denied") is None


async def test_mode_command_persists_permission_mode():
    gw, store, _manager, _gate = _gateway()

    result = await gw.on_command("/mode", "C1", "111.0", "U11111", "plan")

    assert "plan" in result
    row = await store.get_session("C1", "111.0")
    assert row.permission_mode == "plan"


async def test_mode_command_rejects_unknown_mode():
    gw, store, _manager, _gate = _gateway()

    result = await gw.on_command("/mode", "C1", "111.0", "U11111", "not-a-real-mode")

    assert "unknown" in result.lower()
    row = await store.get_session("C1", "111.0")
    assert row is None


async def test_help_command_returns_help_text():
    gw, _store, _manager, _gate = _gateway()

    result = await gw.on_command("/help", "C1", "111.0", "U11111", "")

    assert "/mode" in result


async def test_help_command_uses_the_persona_name():
    gw, _store, _manager, _gate = _gateway(soul=_soul(identity=Identity(name="Anya")))

    result = await gw.on_command("/help", "C1", "111.0", "U11111", "")

    assert result.startswith("Anya commands:")

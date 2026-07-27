from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from jean.db.memory import MemoryStore
from jean.session.session import APPROVAL_WAIT_NOTICE, SLOW_TURN_NOTICE, JeanSession
from jean.session.transcript import LocalTranscripts

FAST_SETTLE = {"settle_timeout": 0.05, "settle_interval": 0.002, "settle_quiet": 0.004}


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    name: str
    id: str = "tu_1"
    input: dict = field(default_factory=dict)


class AssistantMessage:
    def __init__(self, content):
        self.content = content


@dataclass
class FakeResultMessage:
    session_id: str


class FakeChat:
    def __init__(self):
        self.replies: list[str] = []

    async def set_status(self, *a, **k):
        pass

    async def reply(self, channel, thread_ts, text):
        self.replies.append(text)
        return "999.0"

    async def edit(self, *a, **k):
        pass


def _session(chat, *, delay: float, pending: bool, slow: float):
    stream = [
        AssistantMessage([ToolUseBlock(name="mcp__jean_slack__reply")]),
        FakeResultMessage("s1"),
    ]

    class _Client:
        def __init__(self, *, options):
            self.options = options

        async def set_permission_mode(self, mode):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            pass

        async def query(self, t):
            pass

        async def receive_response(self):
            await asyncio.sleep(delay)
            for m in stream:
                yield m

    store = MemoryStore()
    return JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
        client_factory=lambda **kw: _Client(**kw),
        transcripts=store,
        local=LocalTranscripts(cli_home=Path("/nonexistent"), cwd=Path("/w")),
        max_transcript_bytes=32 * 1024 * 1024,
        slow_turn_seconds=slow,
        approval_pending=lambda: pending,
        **FAST_SETTLE,
    )


async def test_a_turn_waiting_on_an_approval_says_so(tmp_path):
    """The bug: the heads-up timer starts at turn start and knows nothing about the
    approval gate, so a turn parked on a human was reported as "still working".
    Observed in production -- "i havent approve anything yet"."""
    chat = FakeChat()
    await _session(chat, delay=0.12, pending=True, slow=0.02).run_turn("do a thing")

    assert chat.replies == [APPROVAL_WAIT_NOTICE]
    assert SLOW_TURN_NOTICE not in chat.replies


async def test_a_turn_actually_working_still_says_it_is_working(tmp_path):
    chat = FakeChat()
    await _session(chat, delay=0.12, pending=False, slow=0.02).run_turn("do a thing")

    assert chat.replies == [SLOW_TURN_NOTICE]


async def test_the_two_notices_do_not_say_the_same_thing():
    """If they were interchangeable the fix would be cosmetic. An approval wait is
    the human's move; a slow turn is jean's."""
    assert APPROVAL_WAIT_NOTICE != SLOW_TURN_NOTICE
    assert "approv" in APPROVAL_WAIT_NOTICE.lower()
    assert "approv" not in SLOW_TURN_NOTICE.lower()


async def test_a_fast_turn_says_nothing_either_way(tmp_path):
    chat = FakeChat()
    await _session(chat, delay=0.0, pending=True, slow=5.0).run_turn("quick")

    assert chat.replies == []


async def test_no_pending_hook_defaults_to_working(tmp_path):
    """Single-process/test wiring may not pass one; it must not crash or claim an
    approval that does not exist."""
    chat = FakeChat()
    store = MemoryStore()

    class _Client:
        def __init__(self, *, options):
            pass

        async def set_permission_mode(self, mode):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            pass

        async def query(self, t):
            pass

        async def receive_response(self):
            await asyncio.sleep(0.12)
            yield FakeResultMessage("s1")

    await JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        options_factory=lambda resume, mode=None: {},
        client_factory=lambda **kw: _Client(**kw),
        transcripts=store,
        local=LocalTranscripts(cli_home=Path("/nonexistent"), cwd=Path("/w")),
        max_transcript_bytes=32 * 1024 * 1024,
        slow_turn_seconds=0.02,
        **FAST_SETTLE,
    ).run_turn("x")

    assert SLOW_TURN_NOTICE in chat.replies

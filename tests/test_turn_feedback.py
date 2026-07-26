from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from jean.db.memory import MemoryStore
from jean.session.session import JeanSession
from jean.session.transcript import LocalTranscripts

FAST_SETTLE = {"settle_timeout": 0.05, "settle_interval": 0.002, "settle_quiet": 0.004}
MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    name: str
    id: str = "tu_1"
    input: dict = field(default_factory=dict)


class AssistantMessage:
    """Under the SDK's own class name -- the NAME is what JeanSession matches on."""

    def __init__(self, content):
        self.content = content


@dataclass
class FakeResultMessage:
    session_id: str


class FakeChat:
    def __init__(self, fail_reply: bool = False):
        self.statuses: list[tuple[str, str, str]] = []
        self.replies: list[tuple[str, str, str]] = []
        self.fail_reply = fail_reply

    async def set_status(self, channel, thread_ts, status):
        self.statuses.append((channel, thread_ts, status))

    async def reply(self, channel, thread_ts, text):
        if self.fail_reply:
            raise RuntimeError("slack down")
        self.replies.append((channel, thread_ts, text))
        return "999.0"

    async def edit(self, *a, **k):
        pass


def _client_factory(stream, delay: float = 0.0):
    class _Client:
        def __init__(self, *, options):
            self.options = options

        async def set_permission_mode(self, mode):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        async def query(self, text):
            pass

        async def receive_response(self):
            if delay:
                await asyncio.sleep(delay)
            for msg in stream:
                yield msg

    return lambda **kw: _Client(**kw)


def _session(chat, stream, tmp_path, *, delay=0.0, **kw):
    store = MemoryStore()
    return JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
        client_factory=_client_factory(stream, delay),
        transcripts=store,
        local=LocalTranscripts(cli_home=tmp_path, cwd=Path("/w")),
        max_transcript_bytes=MAX_TRANSCRIPT_BYTES,
        **FAST_SETTLE,
        **kw,
    )


def _spoke_stream():
    return [
        AssistantMessage([ToolUseBlock(name="mcp__plugin_grafana_grafana__query_prometheus")]),
        AssistantMessage([ToolUseBlock(name="mcp__jean_slack__reply")]),
        FakeResultMessage(session_id="s1"),
    ]


# --- turn-lifecycle logging ------------------------------------------------


async def test_a_completed_turn_logs_its_shape(tmp_path, caplog):
    """ "Is it working?" cost four exchanges in one day because nothing logged a
    turn's duration. `ps` on the CLI child is not an answer -- it stays alive for
    the whole idle window after the turn, so its elapsed time is session age."""
    chat = FakeChat()
    with caplog.at_level(logging.INFO, logger="jean.session"):
        await _session(chat, _spoke_stream(), tmp_path).run_turn("check ES")

    line = " ".join(r.getMessage() for r in caplog.records)
    assert "turn done" in line, line
    for field_ in ("C1", "111.0", "rounds=2", "tools=2", "spoke=True"):
        assert field_ in line, f"{field_!r} missing from {line!r}"
    assert "secs=" in line, line


async def test_a_mute_turn_is_logged_as_such(tmp_path, caplog):
    """spoke=False is the signal that #35's fallback fired -- the one field that
    distinguishes a healthy turn from the silent failure."""
    chat = FakeChat()
    stream = [AssistantMessage([TextBlock("the answer")]), FakeResultMessage(session_id="s1")]
    with caplog.at_level(logging.INFO, logger="jean.session"):
        await _session(chat, stream, tmp_path).run_turn("hi")

    assert "spoke=False" in " ".join(r.getMessage() for r in caplog.records)


async def test_a_failed_turn_still_logs(tmp_path, caplog):
    """A turn that raised is exactly when you want its duration in the log."""

    def _boom(**kw):
        raise RuntimeError("connect failed")

    chat = FakeChat()
    session = _session(chat, [], tmp_path)
    session._client_factory = _boom  # type: ignore[attr-defined]
    with caplog.at_level(logging.INFO, logger="jean.session"), contextlib.suppress(Exception):
        await session.run_turn("hi")

    assert "turn done" in " ".join(r.getMessage() for r in caplog.records)


# --- the slow-turn heads-up ------------------------------------------------


async def test_a_fast_turn_posts_no_heads_up(tmp_path):
    """Quick lookups must stay clean -- an unconditional "on it" would spam every
    one of them, which is why the persona instruction says to skip them."""
    chat = FakeChat()
    await _session(chat, _spoke_stream(), tmp_path, slow_turn_seconds=5.0).run_turn("ping")

    assert chat.replies == []


async def test_a_slow_turn_gets_a_heads_up_while_it_runs(tmp_path):
    """The actual complaint was silence, not duration. After the threshold the
    thread says something, so waiting is legible instead of looking broken."""
    chat = FakeChat()
    await _session(chat, _spoke_stream(), tmp_path, delay=0.12, slow_turn_seconds=0.02).run_turn(
        "long investigation"
    )

    assert len(chat.replies) == 1, chat.replies
    assert chat.replies[0][0:2] == ("C1", "111.0")
    assert chat.replies[0][2].strip() != ""


async def test_the_heads_up_is_cancelled_once_the_turn_finishes(tmp_path):
    """It must never land after the answer: a "still working" note arriving second
    reads as a second, contradictory turn."""
    chat = FakeChat()
    await _session(chat, _spoke_stream(), tmp_path, slow_turn_seconds=0.05).run_turn("hi")
    await asyncio.sleep(0.15)  # well past the threshold

    assert chat.replies == []


async def test_zero_disables_the_heads_up(tmp_path):
    chat = FakeChat()
    await _session(chat, _spoke_stream(), tmp_path, delay=0.08, slow_turn_seconds=0.0).run_turn(
        "hi"
    )

    assert chat.replies == []


async def test_a_failing_heads_up_does_not_lose_the_turn(tmp_path):
    """The note is a nicety; the answer is not. A Slack failure here must not
    propagate into the turn."""
    chat = FakeChat(fail_reply=True)
    await _session(chat, _spoke_stream(), tmp_path, delay=0.1, slow_turn_seconds=0.02).run_turn(
        "hi"
    )
    # No exception, and the turn still completed: status was cleared in `finally`.
    assert ("C1", "111.0", "") in chat.statuses

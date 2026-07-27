from __future__ import annotations

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
    """Stands in for claude_agent_sdk.TextBlock."""

    text: str


@dataclass
class ToolUseBlock:
    """Stands in for claude_agent_sdk.ToolUseBlock."""

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
    def __init__(self):
        self.statuses: list[tuple[str, str, str]] = []
        self.replies: list[tuple[str, str, str]] = []

    async def set_status(self, channel, thread_ts, status):
        self.statuses.append((channel, thread_ts, status))

    async def reply(self, channel, thread_ts, text):
        self.replies.append((channel, thread_ts, text))
        return "999.0"

    async def edit(self, *a, **k):
        pass


def _client_factory(stream):
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
            for msg in stream:
                yield msg

    return lambda **kw: _Client(**kw)


def _session(chat, stream, tmp_path):
    store = MemoryStore()
    return JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
        client_factory=_client_factory(stream),
        transcripts=store,
        local=LocalTranscripts(cli_home=tmp_path, cwd=Path("/w")),
        max_transcript_bytes=MAX_TRANSCRIPT_BYTES,
        **FAST_SETTLE,
    )


# --- the failure this exists for -------------------------------------------


async def test_a_turn_that_never_spoke_still_delivers_its_text(tmp_path, caplog):
    """JEAN_EFFORT=low made the model finish a full investigation and end the turn
    without calling mcp__jean_slack__reply -- the human saw a reaction and nothing
    else, while the answer sat in assistant text jean had already received.

    Delivering it is now the NORMAL path rather than a rescue (it recurred at every
    effort level tried), so this is logged at INFO. The WARNING this test used to
    assert on is gone deliberately."""
    chat = FakeChat()
    stream = [
        AssistantMessage([TextBlock("Root cause: disk watermark at 87%.")]),
        FakeResultMessage(session_id="s1"),
    ]
    with caplog.at_level(logging.INFO, logger="jean.session"):
        await _session(chat, stream, tmp_path).run_turn("why is ES yellow?")

    assert [t for _, _, t in chat.replies] == ["Root cause: disk watermark at 87%."]
    assert "delivered" in " ".join(r.getMessage() for r in caplog.records).lower()
    assert [
        r for r in caplog.records if r.funcName == "_deliver" and r.levelno >= logging.WARNING
    ] == []


async def test_a_turn_that_replied_is_left_alone(tmp_path):
    """The normal path must not double-post: the agent already spoke."""
    chat = FakeChat()
    stream = [
        AssistantMessage([ToolUseBlock(name="mcp__jean_slack__reply")]),
        AssistantMessage([TextBlock("some private scratch text")]),
        FakeResultMessage(session_id="s1"),
    ]
    await _session(chat, stream, tmp_path).run_turn("hi")

    assert chat.replies == []


async def test_upload_counts_as_having_spoken(tmp_path):
    """A file landing in the thread is a visible answer -- do not append prose."""
    chat = FakeChat()
    stream = [
        AssistantMessage([ToolUseBlock(name="mcp__jean_slack__upload")]),
        AssistantMessage([TextBlock("here is the report")]),
        FakeResultMessage(session_id="s1"),
    ]
    await _session(chat, stream, tmp_path).run_turn("make me a report")

    assert chat.replies == []


async def test_a_reaction_alone_does_not_count_as_speaking(tmp_path):
    """react/unreact put an emoji in the thread, never an answer. A turn whose only
    jean_slack call was a reaction is exactly the silent failure."""
    chat = FakeChat()
    stream = [
        AssistantMessage([ToolUseBlock(name="mcp__jean_slack__react")]),
        AssistantMessage([TextBlock("the answer")]),
        FakeResultMessage(session_id="s1"),
    ]
    await _session(chat, stream, tmp_path).run_turn("hi")

    assert [t for _, _, t in chat.replies] == ["the answer"]


async def test_only_the_final_text_is_posted_not_the_running_narration(tmp_path):
    """Mid-turn text is the model thinking out loud between tool calls. Posting all
    of it would dump the working thread on the user; the last message is the answer."""
    chat = FakeChat()
    stream = [
        AssistantMessage([TextBlock("Let me check the cluster.")]),
        AssistantMessage([ToolUseBlock(name="mcp__plugin_grafana_grafana__query_prometheus")]),
        AssistantMessage([TextBlock("Found it. Disk at 87%.")]),
        FakeResultMessage(session_id="s1"),
    ]
    await _session(chat, stream, tmp_path).run_turn("check ES")

    assert [t for _, _, t in chat.replies] == ["Found it. Disk at 87%."]


async def test_multiple_text_blocks_in_the_final_message_are_joined(tmp_path):
    chat = FakeChat()
    stream = [
        AssistantMessage([TextBlock("Root cause:"), TextBlock("disk watermark.")]),
        FakeResultMessage(session_id="s1"),
    ]
    await _session(chat, stream, tmp_path).run_turn("why?")

    assert [t for _, _, t in chat.replies] == ["Root cause:\n\ndisk watermark."]


async def test_a_mute_turn_with_no_text_at_all_says_something(tmp_path, caplog):
    """Nothing to fall back on -- but silence is the one outcome that must never
    reach the user, because it is indistinguishable from jean being broken."""
    chat = FakeChat()
    stream = [
        AssistantMessage([ToolUseBlock(name="Bash")]),
        FakeResultMessage(session_id="s1"),
    ]
    with caplog.at_level(logging.WARNING, logger="jean.session"):
        await _session(chat, stream, tmp_path).run_turn("do a thing")

    assert len(chat.replies) == 1
    assert chat.replies[0][2].strip() != ""


async def test_whitespace_only_text_is_not_a_reply(tmp_path):
    """A blank final message is not an answer -- treat it as no text at all."""
    chat = FakeChat()
    stream = [
        AssistantMessage([TextBlock("   \n  ")]),
        FakeResultMessage(session_id="s1"),
    ]
    await _session(chat, stream, tmp_path).run_turn("hi")

    assert len(chat.replies) == 1
    assert chat.replies[0][2].strip() != ""


async def test_an_unintelligible_stream_does_not_trigger_the_fallback(tmp_path):
    """Zero AssistantMessages means the structural class-name match found nothing --
    most likely claude_agent_sdk renamed the class. In that state jean cannot see
    tool calls either, so `spoke` is False on every turn; firing the fallback would
    append the notice to every real reply the agent made. "I could not tell" must
    read as silence from jean, not as a mute turn."""
    chat = FakeChat()
    stream = [FakeResultMessage(session_id="s1")]
    await _session(chat, stream, tmp_path).run_turn("hi")

    assert chat.replies == []

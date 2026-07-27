from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from jean.db.memory import MemoryStore
from jean.persona.identity import compose_system_prompt
from jean.session.session import JeanSession

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
    def __init__(self, content):
        self.content = content


@dataclass
class FakeResultMessage:
    session_id: str


class FakeChat:
    def __init__(self):
        self.replies: list[tuple[str, str, str]] = []

    async def set_status(self, channel, thread_ts, status):
        pass

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
            for m in stream:
                yield m

    return lambda **kw: _Client(**kw)


def _run(chat, stream, tmp_path):
    from jean.session.transcript import LocalTranscripts

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
        slow_turn_seconds=0.0,
        **FAST_SETTLE,
    ).run_turn("go")


# --- delivering the final text is now normal, not a defect ------------------


async def test_delivering_the_final_text_is_not_logged_as_a_failure(tmp_path, caplog):
    """It happened on every effort level tried, and the message it delivers is
    byte-identical to one the agent posts itself (both go through chat.reply ->
    md_to_mrkdwn -> chunk_text). Reporting it at WARNING trained a reader to treat
    the normal path as broken."""
    chat = FakeChat()
    stream = [AssistantMessage([TextBlock("Root cause: disk watermark.")]), FakeResultMessage("s1")]
    with caplog.at_level(logging.DEBUG, logger="jean.session"):
        await _run(chat, stream, tmp_path)

    assert [t for _, _, t in chat.replies] == ["Root cause: disk watermark."]
    # Filtered by the emitting function, not by substring: the fake writes no
    # .jsonl so the archive step warns regardless, and pytest builds tmp_path from
    # this test's own name -- so any substring like "deliver" matches the PATH
    # inside that unrelated warning. funcName cannot collide.
    delivery_warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.funcName == "_deliver"
    ]
    assert delivery_warnings == [], delivery_warnings


async def test_it_is_still_visible_in_the_log_at_info(tmp_path, caplog):
    """Not silent either -- which path delivered the answer is worth knowing."""
    chat = FakeChat()
    stream = [AssistantMessage([TextBlock("the answer")]), FakeResultMessage("s1")]
    with caplog.at_level(logging.INFO, logger="jean.session"):
        await _run(chat, stream, tmp_path)

    assert "delivered" in " ".join(r.getMessage() for r in caplog.records).lower()


async def test_the_agent_speaking_for_itself_still_wins(tmp_path):
    chat = FakeChat()
    stream = [
        AssistantMessage([ToolUseBlock(name="mcp__jean_slack__reply")]),
        AssistantMessage([TextBlock("private scratch")]),
        FakeResultMessage("s1"),
    ]
    await _run(chat, stream, tmp_path)

    assert chat.replies == []


# --- the promised-a-file guard ---------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Done — here is postmortem.docx with the timeline.",
        "I built deck.pptx for you.",
        "Numbers are in capacity-2026.xlsx.",
        "See the attached report.pdf.",
    ],
)
async def test_naming_a_document_it_never_uploaded_is_flagged(tmp_path, caplog, text):
    """The one thing lost when jean speaks for the agent: `upload` never happened,
    so prose can describe a file that did not arrive. Say so rather than let the
    reader hunt for an attachment."""
    chat = FakeChat()
    stream = [AssistantMessage([TextBlock(text)]), FakeResultMessage("s1")]
    with caplog.at_level(logging.WARNING, logger="jean.session"):
        await _run(chat, stream, tmp_path)

    posted = chat.replies[0][2]
    assert text in posted, "the answer itself must survive intact"
    assert "did not attach" in posted.lower() or "no file" in posted.lower(), posted
    assert "named a file" in " ".join(r.getMessage() for r in caplog.records).lower()


async def test_a_real_upload_is_not_flagged(tmp_path):
    chat = FakeChat()
    stream = [
        AssistantMessage([ToolUseBlock(name="mcp__jean_slack__upload")]),
        AssistantMessage([TextBlock("here is postmortem.docx")]),
        FakeResultMessage("s1"),
    ]
    await _run(chat, stream, tmp_path)

    assert chat.replies == []  # it spoke via upload; nothing to add


async def test_ordinary_filenames_are_not_mistaken_for_deliverables(tmp_path):
    """Investigations mention paths constantly. Only the document types the skills
    actually produce count, or every answer picks up a spurious footnote."""
    chat = FakeChat()
    stream = [
        AssistantMessage([TextBlock("Checked /etc/hosts and values.yaml — both fine.")]),
        FakeResultMessage("s1"),
    ]
    await _run(chat, stream, tmp_path)

    assert len(chat.replies) == 1
    assert "did not attach" not in chat.replies[0][2].lower()


# --- the system prompt must match what jean actually does -------------------


def test_the_prompt_no_longer_claims_plain_text_is_invisible():
    """It said "Anything you say outside of those tool calls is invisible to the
    human". Under this change the final message IS delivered, so that sentence was
    both false and the thing steering the model wrong."""
    prompt = compose_system_prompt("# Persona\n", name="Anya")
    assert "invisible to the human" not in prompt


def test_the_prompt_says_the_final_message_is_delivered():
    prompt = compose_system_prompt("# Persona\n", name="Anya").lower()
    assert "final message" in prompt


def test_the_prompt_still_demands_upload_for_files():
    """The one thing the automatic path cannot do."""
    prompt = compose_system_prompt("# Persona\n", name="Anya")
    assert "mcp__jean_slack__upload" in prompt

from __future__ import annotations

from typing import Any

from jean.slack.mcp import build_slack_mcp

TERMS = frozenset({"acmecorp"})


class FakeChat:
    """Records what actually reached Slack. The point of every test here is that
    this stays empty when a term is present."""

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.edits: list[str] = []
        self.uploads: list[dict[str, Any]] = []

    async def reply(self, channel, thread_ts, text):
        self.replies.append(text)
        return "1.0"

    async def edit(self, channel, ts, text):
        self.edits.append(text)

    async def upload(self, channel, thread_ts, **kw):
        self.uploads.append(kw)

    async def react(self, *a):
        pass

    async def unreact(self, *a):
        pass

    async def set_status(self, *a, **k):
        pass


class FakeGate:
    def __init__(self) -> None:
        self.requested: list[str] = []

    async def request(self, channel, thread_ts, summary):
        self.requested.append(summary)

        class D:
            approved = True
            by = "U1"

        return D()


def _tools(chat, gate, terms=TERMS):
    _, _, tools = build_slack_mcp(chat, gate, channel="C1", thread_ts="1.0", blocked_terms=terms)
    return {t.name: t for t in tools}


async def test_a_reply_carrying_the_term_never_reaches_slack():
    chat, gate = FakeChat(), FakeGate()
    out = await _tools(chat, gate)["reply"].handler({"text": "the acmecorp postmortem is ready"})

    assert out["is_error"] is True
    assert chat.replies == []  # nothing was posted
    assert "acmecorp" in out["content"][0]["text"]  # actionable


async def test_a_clean_reply_still_posts():
    chat, gate = FakeChat(), FakeGate()
    out = await _tools(chat, gate)["reply"].handler({"text": "the postmortem is ready"})

    assert chat.replies == ["the postmortem is ready"]
    assert "is_error" not in out


async def test_an_edit_cannot_smuggle_the_term_in_later():
    """A clean reply followed by an edit would otherwise be a trivial bypass."""
    chat, gate = FakeChat(), FakeGate()
    out = await _tools(chat, gate)["edit"].handler({"ts": "1.0", "text": "for acmecorp"})

    assert out["is_error"] is True
    assert chat.edits == []


async def test_an_upload_is_checked_on_every_field_the_thread_sees():
    chat, gate = FakeChat(), FakeGate()
    tools = _tools(chat, gate)

    for args in (
        {"filename": "acmecorp-deck.pptx"},
        {"filename": "deck.pptx", "title": "acmecorp Q3"},
        {"filename": "deck.pptx", "comment": "as acmecorp asked"},
        {"filename": "notes.md", "content": "acmecorp internal"},
    ):
        out = await tools["upload"].handler(args)
        assert out["is_error"] is True, args

    assert chat.uploads == []


async def test_a_clean_upload_still_goes():
    chat, gate = FakeChat(), FakeGate()
    out = await _tools(chat, gate)["upload"].handler({"filename": "deck.pptx", "title": "Q3"})

    assert len(chat.uploads) == 1
    assert "is_error" not in out


async def test_an_approval_request_cannot_carry_the_term():
    """The summary is posted verbatim, and a blocked term is not something an
    approver may wave through -- so it never becomes a button."""
    chat, gate = FakeChat(), FakeGate()
    out = await _tools(chat, gate)["request_approval"].handler({"summary": "email acmecorp"})

    assert out["is_error"] is True
    assert gate.requested == []


async def test_no_configured_terms_changes_nothing():
    """The default. Every tool behaves exactly as it did before."""
    chat, gate = FakeChat(), FakeGate()
    tools = _tools(chat, gate, terms=frozenset())

    await tools["reply"].handler({"text": "acmecorp is fine here"})
    await tools["request_approval"].handler({"summary": "acmecorp"})

    assert chat.replies == ["acmecorp is fine here"]
    assert gate.requested == ["acmecorp"]


async def test_the_default_argument_is_off():
    """Constructed without the keyword at all -- existing call sites keep working."""
    chat, gate = FakeChat(), FakeGate()
    _, _, tools = build_slack_mcp(chat, gate, channel="C1", thread_ts="1.0")
    by_name = {t.name: t for t in tools}

    await by_name["reply"].handler({"text": "acmecorp"})

    assert chat.replies == ["acmecorp"]

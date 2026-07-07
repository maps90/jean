from __future__ import annotations

from jean.gateway.dispatch import Attachment, build_turn_text, dispatch


class FakeManager:
    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    async def handle(self, channel: str, thread_ts: str, text: str) -> None:
        self.calls.append((channel, thread_ts, text))


def test_build_turn_text_plain_returns_text_unchanged():
    assert build_turn_text("hello", ()) == "hello"


def test_build_turn_text_appends_attachment_blocks():
    attachments = [Attachment(name="report.pdf", path="/tmp/report.pdf")]
    text = build_turn_text("here you go", attachments)
    assert "here you go" in text
    assert '<attachment name="report.pdf" path="/tmp/report.pdf"/>' in text


def test_build_turn_text_appends_multiple_attachments():
    attachments = [
        Attachment(name="a.txt", path="/tmp/a.txt"),
        Attachment(name="b.txt", path="/tmp/b.txt"),
    ]
    text = build_turn_text("files:", attachments)
    assert '<attachment name="a.txt" path="/tmp/a.txt"/>' in text
    assert '<attachment name="b.txt" path="/tmp/b.txt"/>' in text


async def test_dispatch_calls_manager_handle_with_plain_text():
    manager = FakeManager()
    await dispatch(manager, channel="C1", thread_ts="111.0", text="hi")
    assert manager.calls == [("C1", "111.0", "hi")]


async def test_dispatch_calls_manager_handle_with_attachment_text():
    manager = FakeManager()
    attachments = [Attachment(name="a.txt", path="/tmp/a.txt")]
    await dispatch(manager, channel="C1", thread_ts="111.0", text="hi", attachments=attachments)
    channel, thread_ts, text = manager.calls[0]
    assert channel == "C1"
    assert thread_ts == "111.0"
    assert "hi" in text
    assert "a.txt" in text

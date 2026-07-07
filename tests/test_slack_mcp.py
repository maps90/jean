from __future__ import annotations

from dataclasses import dataclass

from jean.approval.gate import ApprovalGate
from jean.db.memory import MemoryStore
from jean.persona.model import ApproverEntry
from jean.slack.mcp import build_slack_mcp


@dataclass
class _FakeRoutingContext:
    """Stand-in for session.session.RoutingContext (Task 14): a mutable
    channel/thread_ts the mcp tools read via channel_of()/thread_of()."""

    channel: str = ""
    thread_ts: str = ""


class FakeChat:
    def __init__(self):
        self.replies: list[tuple[str, str, str]] = []
        self.edits: list[tuple[str, str, str]] = []
        self.uploads: list[dict] = []
        self.reacts: list[tuple[str, str, str]] = []
        self.unreacts: list[tuple[str, str, str]] = []

    async def reply(self, channel, thread_ts, text):
        self.replies.append((channel, thread_ts, text))
        return "999.1"

    async def edit(self, channel, ts, text):
        self.edits.append((channel, ts, text))

    async def upload(
        self, channel, thread_ts, *, path=None, content=None, filename, title=None, comment=None
    ):
        self.uploads.append(
            {
                "channel": channel,
                "thread_ts": thread_ts,
                "path": path,
                "content": content,
                "filename": filename,
                "title": title,
                "comment": comment,
            }
        )

    async def react(self, channel, ts, emoji):
        self.reacts.append((channel, ts, emoji))

    async def unreact(self, channel, ts, emoji):
        self.unreacts.append((channel, ts, emoji))

    async def set_status(self, channel, thread_ts, status):
        pass


def _make_gate(approved: bool):
    coordinator = MemoryStore()
    approvers = [ApproverEntry(user_id="U11111", scope="", catchall=True)]

    async def post_blocks(channel, thread_ts, text, blocks):
        return "1000.1"

    gate = ApprovalGate(
        post_blocks, coordinator, approvers_provider=lambda: approvers, timeout_seconds=5
    )

    async def fake_request(channel, thread_ts, summary):
        approval_id = "fixed-id"
        await coordinator.create(approval_id, channel, thread_ts, summary)
        await coordinator.set_approvers(approval_id, {"U11111"})
        await coordinator.resolve(approval_id, approved, "U11111")
        return await coordinator.wait(approval_id, 5)

    gate.request = fake_request  # type: ignore[method-assign]
    return gate


def _routing_context(channel="C1", thread_ts="111.0"):
    return _FakeRoutingContext(channel=channel, thread_ts=thread_ts)


def test_tool_names_are_namespaced_for_allowed_tools():
    chat = FakeChat()
    gate = _make_gate(True)
    ctx = _routing_context()
    _server, tool_names, tools = build_slack_mcp(
        chat, gate, channel_of=lambda: ctx.channel, thread_of=lambda: ctx.thread_ts
    )
    names = {t.name for t in tools}
    assert names == {"reply", "edit", "upload", "react", "unreact", "request_approval"}
    assert set(tool_names) == {f"mcp__jean_slack__{n}" for n in names}


async def test_reply_tool_calls_chat_with_routed_channel_and_thread():
    chat = FakeChat()
    gate = _make_gate(True)
    ctx = _routing_context("C9", "222.0")
    _server, _names, tools = build_slack_mcp(
        chat, gate, channel_of=lambda: ctx.channel, thread_of=lambda: ctx.thread_ts
    )
    reply_tool = next(t for t in tools if t.name == "reply")

    result = await reply_tool.handler({"text": "hello there"})

    assert chat.replies == [("C9", "222.0", "hello there")]
    assert result["content"][0]["type"] == "text"
    assert "999.1" in result["content"][0]["text"]


async def test_edit_tool():
    chat = FakeChat()
    gate = _make_gate(True)
    ctx = _routing_context()
    _server, _names, tools = build_slack_mcp(
        chat, gate, channel_of=lambda: ctx.channel, thread_of=lambda: ctx.thread_ts
    )
    edit_tool = next(t for t in tools if t.name == "edit")
    await edit_tool.handler({"ts": "111.5", "text": "updated"})
    assert chat.edits == [("C1", "111.5", "updated")]


async def test_upload_tool():
    chat = FakeChat()
    gate = _make_gate(True)
    ctx = _routing_context()
    _server, _names, tools = build_slack_mcp(
        chat, gate, channel_of=lambda: ctx.channel, thread_of=lambda: ctx.thread_ts
    )
    upload_tool = next(t for t in tools if t.name == "upload")
    await upload_tool.handler({"filename": "x.txt", "content": "hi"})
    assert chat.uploads[0]["filename"] == "x.txt"
    assert chat.uploads[0]["content"] == "hi"
    assert chat.uploads[0]["channel"] == "C1"


async def test_react_and_unreact_tools():
    chat = FakeChat()
    gate = _make_gate(True)
    ctx = _routing_context()
    _server, _names, tools = build_slack_mcp(
        chat, gate, channel_of=lambda: ctx.channel, thread_of=lambda: ctx.thread_ts
    )
    react_tool = next(t for t in tools if t.name == "react")
    unreact_tool = next(t for t in tools if t.name == "unreact")
    await react_tool.handler({"ts": "111.9", "emoji": "eyes"})
    await unreact_tool.handler({"ts": "111.9", "emoji": "eyes"})
    assert chat.reacts == [("C1", "111.9", "eyes")]
    assert chat.unreacts == [("C1", "111.9", "eyes")]


async def test_request_approval_tool_approved():
    chat = FakeChat()
    gate = _make_gate(True)
    ctx = _routing_context()
    _server, _names, tools = build_slack_mcp(
        chat, gate, channel_of=lambda: ctx.channel, thread_of=lambda: ctx.thread_ts
    )
    request_approval_tool = next(t for t in tools if t.name == "request_approval")
    result = await request_approval_tool.handler({"summary": "deploy the thing"})
    text = result["content"][0]["text"]
    assert "approved" in text
    assert "U11111" in text


async def test_request_approval_tool_denied():
    chat = FakeChat()
    gate = _make_gate(False)
    ctx = _routing_context()
    _server, _names, tools = build_slack_mcp(
        chat, gate, channel_of=lambda: ctx.channel, thread_of=lambda: ctx.thread_ts
    )
    request_approval_tool = next(t for t in tools if t.name == "request_approval")
    result = await request_approval_tool.handler({"summary": "delete prod"})
    assert "denied" in result["content"][0]["text"]

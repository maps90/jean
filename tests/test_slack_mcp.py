from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from jean.approval.gate import ApprovalGate
from jean.db.memory import MemoryStore
from jean.persona.model import ApproverEntry
from jean.ports import ChatReadError, Message
from jean.slack.mcp import build_slack_mcp


class FakeChat:
    def __init__(self):
        self.replies: list[tuple[str, str, str]] = []
        self.edits: list[tuple[str, str, str]] = []
        self.uploads: list[dict] = []
        self.reacts: list[tuple[str, str, str]] = []
        self.unreacts: list[tuple[str, str, str]] = []
        self.resolved: list[str] = []
        self.history_calls: list[dict] = []
        self.replies_calls: list[dict] = []
        self.history_result: list[Message] = []
        self.history_has_more = False
        self.replies_result: list[Message] = []

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

    async def resolve_channel(self, name_or_id):
        self.resolved.append(name_or_id)
        if name_or_id.strip("#") == "nope":
            raise ChatReadError("channel_not_found", "no public channel named 'nope'")
        return "C-RESOLVED"

    async def history(self, channel, *, oldest=None, latest=None, limit=50):
        self.history_calls.append(
            {"channel": channel, "oldest": oldest, "latest": latest, "limit": limit}
        )
        return (self.history_result, self.history_has_more)

    async def thread_replies(self, channel, thread_ts, *, limit=50):
        self.replies_calls.append({"channel": channel, "thread_ts": thread_ts, "limit": limit})
        return (self.replies_result, False)


def _make_gate(approved: bool):
    coordinator = MemoryStore()
    approvers = [ApproverEntry(user_id="U11111", scope="", catchall=True)]

    async def post_blocks(channel, thread_ts, text, blocks):
        return "1000.1"

    async def update_blocks(channel, ts, text, blocks):
        return None

    gate = ApprovalGate(
        post_blocks,
        coordinator,
        update_blocks=update_blocks,
        approvers_provider=lambda: approvers,
        timeout_seconds=5,
    )

    async def fake_request(channel, thread_ts, summary):
        approval_id = "fixed-id"
        await coordinator.create(approval_id, channel, thread_ts, summary)
        await coordinator.set_approvers(approval_id, {"U11111"})
        await coordinator.resolve(approval_id, approved, "U11111")
        return await coordinator.wait(approval_id, 5)

    gate.request = fake_request  # type: ignore[method-assign]
    return gate


def test_tool_names_are_namespaced_for_allowed_tools():
    chat = FakeChat()
    gate = _make_gate(True)
    _server, tool_names, tools = build_slack_mcp(chat, gate, channel="C1", thread_ts="111.0")
    names = {t.name for t in tools}
    assert names == {
        "reply",
        "edit",
        "upload",
        "react",
        "unreact",
        "request_approval",
        "read_channel",
        "read_thread",
    }
    assert set(tool_names) == {f"mcp__jean_slack__{n}" for n in names}


async def test_reply_tool_calls_chat_with_bound_channel_and_thread():
    chat = FakeChat()
    gate = _make_gate(True)
    _server, _names, tools = build_slack_mcp(chat, gate, channel="C9", thread_ts="222.0")
    reply_tool = next(t for t in tools if t.name == "reply")

    result = await reply_tool.handler({"text": "hello there"})

    assert chat.replies == [("C9", "222.0", "hello there")]
    assert result["content"][0]["type"] == "text"
    assert "999.1" in result["content"][0]["text"]


async def test_edit_tool():
    chat = FakeChat()
    gate = _make_gate(True)
    _server, _names, tools = build_slack_mcp(chat, gate, channel="C1", thread_ts="111.0")
    edit_tool = next(t for t in tools if t.name == "edit")
    await edit_tool.handler({"ts": "111.5", "text": "updated"})
    assert chat.edits == [("C1", "111.5", "updated")]


async def test_upload_tool():
    chat = FakeChat()
    gate = _make_gate(True)
    _server, _names, tools = build_slack_mcp(chat, gate, channel="C1", thread_ts="111.0")
    upload_tool = next(t for t in tools if t.name == "upload")
    await upload_tool.handler({"filename": "x.txt", "content": "hi"})
    assert chat.uploads[0]["filename"] == "x.txt"
    assert chat.uploads[0]["content"] == "hi"
    assert chat.uploads[0]["channel"] == "C1"


async def test_react_and_unreact_tools():
    chat = FakeChat()
    gate = _make_gate(True)
    _server, _names, tools = build_slack_mcp(chat, gate, channel="C1", thread_ts="111.0")
    react_tool = next(t for t in tools if t.name == "react")
    unreact_tool = next(t for t in tools if t.name == "unreact")
    await react_tool.handler({"ts": "111.9", "emoji": "eyes"})
    await unreact_tool.handler({"ts": "111.9", "emoji": "eyes"})
    assert chat.reacts == [("C1", "111.9", "eyes")]
    assert chat.unreacts == [("C1", "111.9", "eyes")]


async def test_request_approval_tool_approved():
    chat = FakeChat()
    gate = _make_gate(True)
    _server, _names, tools = build_slack_mcp(chat, gate, channel="C1", thread_ts="111.0")
    request_approval_tool = next(t for t in tools if t.name == "request_approval")
    result = await request_approval_tool.handler({"summary": "deploy the thing"})
    text = result["content"][0]["text"]
    assert "approved" in text
    assert "U11111" in text


async def test_request_approval_tool_denied():
    chat = FakeChat()
    gate = _make_gate(False)
    _server, _names, tools = build_slack_mcp(chat, gate, channel="C1", thread_ts="111.0")
    request_approval_tool = next(t for t in tools if t.name == "request_approval")
    result = await request_approval_tool.handler({"summary": "delete prod"})
    assert "denied" in result["content"][0]["text"]


async def test_two_sessions_reply_to_their_own_thread_no_shared_routing():
    """Each session builds its OWN Slack MCP server bound to that thread. There
    is no process-wide routing slot for a concurrently-running turn on another
    thread to repoint, so a slow turn on thread A always replies in A even after
    thread B has started -- the "message jumps to another thread" bug.

    Reproduces the race: build A, build B, then let A's reply fire *after* B was
    built and used. With shared routing, A's reply would land in B's thread."""
    chat = FakeChat()
    gate = _make_gate(True)

    _sa, _na, tools_a = build_slack_mcp(chat, gate, channel="C-A", thread_ts="111.0")
    _sb, _nb, tools_b = build_slack_mcp(chat, gate, channel="C-B", thread_ts="222.0")

    reply_a = next(t for t in tools_a if t.name == "reply")
    reply_b = next(t for t in tools_b if t.name == "reply")

    # B starts a turn and replies, then A (the slow turn) finally replies.
    await reply_b.handler({"text": "for B"})
    await reply_a.handler({"text": "for A"})

    assert chat.replies == [("C-B", "222.0", "for B"), ("C-A", "111.0", "for A")]


def _tools_for(chat):
    _server, _names, tools = build_slack_mcp(chat, _make_gate(True), channel="C1", thread_ts="1.0")
    return {t.name: t for t in tools}


async def test_read_tools_are_exposed():
    tools = _tools_for(FakeChat())
    assert "read_channel" in tools
    assert "read_thread" in tools


async def test_read_channel_resolves_the_name_and_renders_the_messages():
    chat = FakeChat()
    chat.history_result = [Message(ts="1754210040.0", user="U9", text="pods down")]
    result = await _tools_for(chat)["read_channel"].handler({"channel": "#sre-support"})

    assert chat.resolved == ["#sre-support"]
    assert chat.history_calls[0]["channel"] == "C-RESOLVED"
    text = result["content"][0]["text"]
    assert "<@U9>" in text
    assert "pods down" in text
    assert "ts=1754210040.0" in text


async def test_read_channel_turns_a_bare_date_into_a_whole_local_day():
    """This is the 'today's messages' path: one date in both slots."""
    chat = FakeChat()
    await _tools_for(chat)["read_channel"].handler(
        {
            "channel": "C1",
            "oldest": "2026-08-03",
            "latest": "2026-08-03",
            "timezone": "Asia/Jakarta",
        }
    )
    call = chat.history_calls[0]
    assert call["latest"] - call["oldest"] == pytest.approx(86399.999999, abs=1e-3)


async def test_read_channel_defaults_to_utc_when_no_timezone_given():
    chat = FakeChat()
    await _tools_for(chat)["read_channel"].handler({"channel": "C1", "oldest": "2026-08-03"})
    expected = datetime(2026, 8, 3, tzinfo=ZoneInfo("UTC")).timestamp()
    assert chat.history_calls[0]["oldest"] == expected


async def test_read_channel_states_truncation_instead_of_hiding_it():
    chat = FakeChat()
    chat.history_result = [Message(ts="1754210040.0", user="U9", text="x")]
    chat.history_has_more = True
    result = await _tools_for(chat)["read_channel"].handler({"channel": "C1"})
    assert "older messages" in result["content"][0]["text"].lower()


async def test_read_channel_reports_an_unreadable_channel_as_an_error():
    """not_in_channel must reach the agent as an error, never as 'no messages'."""
    chat = FakeChat()
    result = await _tools_for(chat)["read_channel"].handler({"channel": "#nope"})
    assert result.get("is_error") is True
    assert "channel_not_found" in result["content"][0]["text"]


async def test_read_channel_rejects_an_unparseable_date_without_calling_slack():
    chat = FakeChat()
    result = await _tools_for(chat)["read_channel"].handler(
        {"channel": "C1", "oldest": "last tuesday"}
    )
    assert result.get("is_error") is True
    assert chat.history_calls == []


async def test_read_thread_passes_the_parent_ts_through():
    chat = FakeChat()
    chat.replies_result = [
        Message(ts="1754210040.0", user="U9", text="parent"),
        Message(ts="1754210100.0", user="U8", text="answer"),
    ]
    result = await _tools_for(chat)["read_thread"].handler(
        {"channel": "#sre-support", "thread_ts": "1754210040.0"}
    )
    call = chat.replies_calls[0]
    assert call["channel"] == "C-RESOLVED"
    assert call["thread_ts"] == "1754210040.0"
    text = result["content"][0]["text"]
    assert text.index("parent") < text.index("answer")


async def test_read_tools_never_ask_for_approval():
    """A read that blocked on a human would defeat the feature; Slack's own
    membership check is the authorization."""
    chat = FakeChat()
    gate_calls = []

    class RecordingGate:
        async def request(self, channel, thread_ts, summary):
            gate_calls.append(summary)
            raise AssertionError("reads must not go through the approval gate")

    _s, _n, tools = build_slack_mcp(chat, RecordingGate(), channel="C1", thread_ts="1.0")
    by_name = {t.name: t for t in tools}
    await by_name["read_channel"].handler({"channel": "C1"})
    await by_name["read_thread"].handler({"channel": "C1", "thread_ts": "1.0"})
    assert gate_calls == []

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool

from jean.approval.gate import ApprovalGate
from jean.ports import ChatSurface

# Read the currently-routed channel/thread for this turn (see
# session.session.RoutingContext) -- the tools themselves are stateless.
ChannelOf = Callable[[], str]
ThreadOf = Callable[[], str]

_UPLOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "filename": {"type": "string"},
        "path": {"type": "string", "description": "local filesystem path to upload"},
        "content": {"type": "string", "description": "inline file content"},
        "title": {"type": "string"},
        "comment": {"type": "string"},
    },
    "required": ["filename"],
}


def build_slack_mcp(
    chat: ChatSurface,
    gate: ApprovalGate,
    *,
    channel_of: ChannelOf,
    thread_of: ThreadOf,
) -> tuple[Any, list[str], list[SdkMcpTool]]:
    """Build the in-process `jean_slack` MCP server. Each tool's logic lives
    in a plain module-level-shaped async fn (`_reply` etc.) so it is testable
    by calling `<tool>.handler(args)` directly, without going through the SDK
    wrapper (see tests/test_slack_mcp.py)."""

    async def _reply(args: dict[str, Any]) -> dict[str, Any]:
        ts = await chat.reply(channel_of(), thread_of(), args["text"])
        return {"content": [{"type": "text", "text": f"posted (ts={ts})"}]}

    async def _edit(args: dict[str, Any]) -> dict[str, Any]:
        await chat.edit(channel_of(), args["ts"], args["text"])
        return {"content": [{"type": "text", "text": "edited"}]}

    async def _upload(args: dict[str, Any]) -> dict[str, Any]:
        await chat.upload(
            channel_of(),
            thread_of(),
            path=args.get("path"),
            content=args.get("content"),
            filename=args["filename"],
            title=args.get("title"),
            comment=args.get("comment"),
        )
        return {"content": [{"type": "text", "text": f"uploaded {args['filename']}"}]}

    async def _react(args: dict[str, Any]) -> dict[str, Any]:
        await chat.react(channel_of(), args["ts"], args["emoji"])
        return {"content": [{"type": "text", "text": "reacted"}]}

    async def _unreact(args: dict[str, Any]) -> dict[str, Any]:
        await chat.unreact(channel_of(), args["ts"], args["emoji"])
        return {"content": [{"type": "text", "text": "unreacted"}]}

    async def _request_approval(args: dict[str, Any]) -> dict[str, Any]:
        decision = await gate.request(channel_of(), thread_of(), args["summary"])
        verb = "approved" if decision.approved else "denied"
        return {"content": [{"type": "text", "text": f"{verb} by {decision.by}"}]}

    tools = [
        tool("reply", "Reply in the current Slack thread. Text is markdown.", {"text": str})(
            _reply
        ),
        tool("edit", "Edit a message previously sent by reply/upload.", {"ts": str, "text": str})(
            _edit
        ),
        tool("upload", "Upload a file to the current Slack thread.", _UPLOAD_SCHEMA)(_upload),
        tool("react", "Add an emoji reaction to a message.", {"ts": str, "emoji": str})(_react),
        tool("unreact", "Remove an emoji reaction from a message.", {"ts": str, "emoji": str})(
            _unreact
        ),
        tool(
            "request_approval",
            "Ask a human approver before taking a mutating/side-effecting action. "
            "Blocks until a decision is made or the request times out.",
            {"summary": str},
        )(_request_approval),
    ]

    server = create_sdk_mcp_server("jean_slack", tools=tools)
    tool_names = [f"mcp__jean_slack__{t.name}" for t in tools]
    return server, tool_names, tools

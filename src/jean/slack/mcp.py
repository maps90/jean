from __future__ import annotations

from typing import Any

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool

from jean.approval.gate import ApprovalGate
from jean.ports import ChatReadError, ChatSurface
from jean.slack.render import render_messages
from jean.slack.timewindow import TimeWindowError, parse_bound

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

_CHANNEL_PROP = {
    "type": "string",
    "description": "channel name (#sre-support or sre-support) or id (C0123ABC)",
}

_READ_CHANNEL_SCHEMA = {
    "type": "object",
    "properties": {
        "channel": _CHANNEL_PROP,
        "oldest": {
            "type": "string",
            "description": (
                "start of the window: epoch seconds, 'YYYY-MM-DD' (that whole "
                "local day), or 'YYYY-MM-DDTHH:MM'"
            ),
        },
        "latest": {"type": "string", "description": "end of the window, same forms as oldest"},
        "timezone": {
            "type": "string",
            "description": "IANA timezone dates are read in, e.g. 'Asia/Jakarta'. Default UTC.",
        },
        "limit": {"type": "integer", "description": "max messages, default 50, capped at 200"},
    },
    "required": ["channel"],
}

_READ_THREAD_SCHEMA = {
    "type": "object",
    "properties": {
        "channel": _CHANNEL_PROP,
        "thread_ts": {
            "type": "string",
            "description": "the parent message's ts, as printed by read_channel",
        },
        "timezone": {
            "type": "string",
            "description": (
                "IANA timezone timestamps are shown in, e.g. 'Asia/Jakarta'. Default UTC."
            ),
        },
        "limit": {"type": "integer", "description": "max messages, default 50, capped at 200"},
    },
    "required": ["channel", "thread_ts"],
}


def build_slack_mcp(
    chat: ChatSurface,
    gate: ApprovalGate,
    *,
    channel: str,
    thread_ts: str,
) -> tuple[Any, list[str], list[SdkMcpTool]]:
    """Build the in-process `jean_slack` MCP server for ONE Slack thread.

    channel/thread_ts are bound here, at construction, rather than read from a
    process-wide slot at call time. One server per session is the whole point:
    the SDK invokes these tools lazily -- long after the turn began -- and a
    single shared server would have every worker's threads reading one mutable
    routing slot, so a slow turn on thread A that replies after thread B has
    started would post into B. Binding per session removes that shared state
    (same reasoning as agent_options.build_can_use_tool). Building a server per
    session is cheap: it is in-process closures, not a child process.

    Each tool's logic lives in a plain module-level-shaped async fn (`_reply`
    etc.) so it is testable by calling `<tool>.handler(args)` directly, without
    going through the SDK wrapper (see tests/test_slack_mcp.py).

    The read tools are the one exception to the per-thread binding above: they
    take an explicit `channel`, because reading somewhere OTHER than this thread
    is the whole point of them. They carry no approval gate -- a read mutates
    nothing, and Slack itself is the authorization (conversations.history fails
    with not_in_channel unless this app was invited), so the decision never
    reaches the model."""

    async def _reply(args: dict[str, Any]) -> dict[str, Any]:
        ts = await chat.reply(channel, thread_ts, args["text"])
        return {"content": [{"type": "text", "text": f"posted (ts={ts})"}]}

    async def _edit(args: dict[str, Any]) -> dict[str, Any]:
        await chat.edit(channel, args["ts"], args["text"])
        return {"content": [{"type": "text", "text": "edited"}]}

    async def _upload(args: dict[str, Any]) -> dict[str, Any]:
        await chat.upload(
            channel,
            thread_ts,
            path=args.get("path"),
            content=args.get("content"),
            filename=args["filename"],
            title=args.get("title"),
            comment=args.get("comment"),
        )
        return {"content": [{"type": "text", "text": f"uploaded {args['filename']}"}]}

    async def _react(args: dict[str, Any]) -> dict[str, Any]:
        await chat.react(channel, args["ts"], args["emoji"])
        return {"content": [{"type": "text", "text": "reacted"}]}

    async def _unreact(args: dict[str, Any]) -> dict[str, Any]:
        await chat.unreact(channel, args["ts"], args["emoji"])
        return {"content": [{"type": "text", "text": "unreacted"}]}

    async def _request_approval(args: dict[str, Any]) -> dict[str, Any]:
        decision = await gate.request(channel, thread_ts, args["summary"])
        verb = "approved" if decision.approved else "denied"
        return {"content": [{"type": "text", "text": f"{verb} by {decision.by}"}]}

    def _error(text: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": text}], "is_error": True}

    async def _read_channel(args: dict[str, Any]) -> dict[str, Any]:
        timezone = str(args.get("timezone") or "UTC")
        try:
            # The same bare date means midnight as a start and end-of-day as an
            # end, so "today" is one call with one date in both slots.
            oldest = parse_bound(args.get("oldest"), timezone=timezone, end_of_day=False)
            latest = parse_bound(args.get("latest"), timezone=timezone, end_of_day=True)
        except TimeWindowError as exc:
            return _error(str(exc))
        try:
            target = await chat.resolve_channel(str(args["channel"]))
            messages, has_more = await chat.history(
                target,
                oldest=oldest,
                latest=latest,
                limit=int(args.get("limit") or 50),
            )
            rendered = render_messages(messages, timezone=timezone, truncated=has_more)
        except ChatReadError as exc:
            return _error(f"could not read that channel ({exc.code}): {exc}")
        except TimeWindowError as exc:  # a bad timezone with no date bounds
            return _error(str(exc))
        return {"content": [{"type": "text", "text": rendered}]}

    async def _read_thread(args: dict[str, Any]) -> dict[str, Any]:
        timezone = str(args.get("timezone") or "UTC")
        try:
            target = await chat.resolve_channel(str(args["channel"]))
            messages, has_more = await chat.thread_replies(
                target, str(args["thread_ts"]), limit=int(args.get("limit") or 50)
            )
            rendered = render_messages(messages, timezone=timezone, truncated=has_more)
        except ChatReadError as exc:
            return _error(f"could not read that thread ({exc.code}): {exc}")
        except TimeWindowError as exc:  # an unknown timezone reaches rendering
            return _error(str(exc))
        return {"content": [{"type": "text", "text": rendered}]}

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
        tool(
            "read_channel",
            "Read recent messages from a public Slack channel this app has been "
            "invited to. Returns each message with its ts, which you pass to "
            "read_thread to open a thread.",
            _READ_CHANNEL_SCHEMA,
        )(_read_channel),
        tool(
            "read_thread",
            "Read the replies under one message, given the channel and the parent message's ts.",
            _READ_THREAD_SCHEMA,
        )(_read_thread),
    ]

    server = create_sdk_mcp_server("jean_slack", tools=tools)
    tool_names = [f"mcp__jean_slack__{t.name}" for t in tools]
    return server, tool_names, tools

from __future__ import annotations

from typing import Any

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool

from jean.approval.blocked_terms import find_blocked, refusal
from jean.approval.gate import ApprovalGate
from jean.ports import ChatSurface

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
    channel: str,
    thread_ts: str,
    blocked_terms: frozenset[str] = frozenset(),
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
    going through the SDK wrapper (see tests/test_slack_mcp.py)."""

    def _blocked(*texts: str | None) -> dict[str, Any] | None:
        """The last checkpoint before words leave the process.

        This is where the rule has to live to be a rule at all: these tools are in
        `allowed_tools`, so the SDK never routes them through `can_use_tool` and the
        risk classifier never sees them. Returning `is_error` rather than raising
        keeps it a normal tool failure the agent can read and retry from.
        """
        for text in texts:
            term = find_blocked(text or "", blocked_terms)
            if term:
                return {"content": [{"type": "text", "text": refusal(term)}], "is_error": True}
        return None

    async def _reply(args: dict[str, Any]) -> dict[str, Any]:
        if stop := _blocked(args["text"]):
            return stop
        ts = await chat.reply(channel, thread_ts, args["text"])
        return {"content": [{"type": "text", "text": f"posted (ts={ts})"}]}

    async def _edit(args: dict[str, Any]) -> dict[str, Any]:
        if stop := _blocked(args["text"]):
            return stop
        await chat.edit(channel, args["ts"], args["text"])
        return {"content": [{"type": "text", "text": "edited"}]}

    async def _upload(args: dict[str, Any]) -> dict[str, Any]:
        # `path` is not checked: it names a local file, and a file jean authored
        # could not have contained a blocked term (the classifier denies writing
        # one). Its NAME is not published to the thread -- `filename` is.
        if stop := _blocked(
            args["filename"], args.get("title"), args.get("comment"), args.get("content")
        ):
            return stop
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
        # The summary is posted to the thread verbatim, so it is a post like any
        # other -- and a blocked term is not something an approver may wave through.
        if stop := _blocked(args["summary"]):
            return stop
        decision = await gate.request(channel, thread_ts, args["summary"])
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

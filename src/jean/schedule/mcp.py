from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool

from jean.approval.gate import ApprovalGate
from jean.ports import ScheduleStore
from jean.schedule.cron import CronError, next_after, validate

_CREATE_SCHEMA = {
    "type": "object",
    "properties": {
        "cron": {
            "type": "string",
            "description": "5-field cron, e.g. '0 9 * * 2' for Tuesdays at 09:00",
        },
        "timezone": {
            "type": "string",
            "description": "IANA timezone the cron is read in, e.g. 'Asia/Jakarta'",
        },
        "prompt": {
            "type": "string",
            "description": "what to do each time it fires, phrased as an instruction",
        },
    },
    "required": ["cron", "timezone", "prompt"],
}


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def build_schedule_mcp(
    store: ScheduleStore,
    gate: ApprovalGate,
    *,
    channel: str,
    thread_ts: str,
    clock: Callable[[], float] = time.time,
) -> tuple[Any, list[str], list[SdkMcpTool]]:
    """Build the in-process `jean_schedule` MCP server for ONE Slack thread.

    channel/thread_ts are bound here, at construction, exactly as in
    build_slack_mcp: the SDK invokes these tools lazily, so reading a shared slot
    at call time would let a slow turn on thread A write a schedule into thread B.

    The approval gate is called INSIDE create/remove. An agent cannot write a row
    by choosing not to ask -- that decision lives in code, never in the prompt.
    """

    def _describe(cron: str, timezone: str, when: float) -> str:
        local = datetime.fromtimestamp(when, ZoneInfo(timezone))
        return f"`{cron}` ({timezone}), next run {local:%a %d %b %H:%M}"

    async def _create(args: dict[str, Any]) -> dict[str, Any]:
        cron = str(args.get("cron", ""))
        timezone = str(args.get("timezone", ""))
        prompt = str(args.get("prompt", ""))
        if not prompt.strip():
            return _err("prompt is required")
        # Validate BEFORE asking a human: nobody should be shown Approve/Deny
        # for a schedule that could never run.
        try:
            validate(cron, timezone)
            when = next_after(cron, timezone, clock())
        except CronError as exc:
            return _err(str(exc))

        decision = await gate.request(
            channel,
            thread_ts,
            f"create a schedule: {_describe(cron, timezone, when)} -- {prompt}",
        )
        if not decision.approved:
            return _err("Schedule not created: the request was denied.")

        row = await store.create_schedule(
            channel=channel,
            thread_ts=thread_ts,
            cron=cron,
            timezone=timezone,
            prompt=prompt,
            created_by=decision.by,
            next_run_at=when,
        )
        return _ok(f"Scheduled ({row.id}): {_describe(cron, timezone, when)}")

    async def _list(_args: dict[str, Any]) -> dict[str, Any]:
        rows = await store.list_schedules(channel, thread_ts)
        if not rows:
            return _ok("No schedules in this thread.")
        lines = [
            f"{r.id}: {_describe(r.cron, r.timezone, r.next_run_at)} -- {r.prompt}"
            + (f" [last: {r.last_status}]" if r.last_status else "")
            for r in rows
        ]
        return _ok("\n".join(lines))

    async def _remove(args: dict[str, Any]) -> dict[str, Any]:
        schedule_id = str(args.get("id", ""))
        rows = await store.list_schedules(channel, thread_ts)
        match = next((r for r in rows if r.id == schedule_id), None)
        # Thread-scoped lookup, and it happens BEFORE the approval: raising
        # Approve/Deny for someone else's id would confirm that it exists.
        if match is None:
            return _err(f"No schedule {schedule_id!r} in this thread.")

        decision = await gate.request(
            channel,
            thread_ts,
            f"remove the schedule {_describe(match.cron, match.timezone, match.next_run_at)}",
        )
        if not decision.approved:
            return _err("Schedule not removed: the request was denied.")

        await store.delete_schedule(schedule_id, channel, thread_ts)
        return _ok(f"Removed schedule {schedule_id}.")

    tools = [
        tool(
            "create",
            "Create a recurring prompt in this thread. Asks a human to approve first.",
            _CREATE_SCHEMA,
        )(_create),
        tool("list", "List the schedules in this thread.", {})(_list),
        tool(
            "remove",
            "Remove a schedule from this thread. Asks a human to approve first.",
            {"id": str},
        )(_remove),
    ]
    server = create_sdk_mcp_server("jean_schedule", tools=tools)
    names = [f"mcp__jean_schedule__{t.name}" for t in tools]
    return server, names, tools

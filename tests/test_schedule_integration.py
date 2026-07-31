"""End-to-end across the schedule feature's own seams.

Everything jean owns is real here -- MemoryStore, build_schedule_mcp, cron, and
ScheduleRunner. Only the two things outside the feature are faked: the approval
gate (a human clicking a button) and SessionManager.handle (running a turn).

The per-component tests each prove one piece against fakes on both sides. This
proves the pieces agree with each other -- that what create() writes is what the
runner later claims, and that the prompt reaches the thread it was created in.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from jean.db.memory import MemoryStore
from jean.ports import ApprovalDecision
from jean.schedule.mcp import build_schedule_mcp
from jean.schedule.runner import ScheduleRunner


class FakeGate:
    def __init__(self, approved: bool = True) -> None:
        self.summaries: list[str] = []
        self._approved = approved

    async def request(self, channel: str, thread_ts: str, summary: str) -> ApprovalDecision:
        self.summaries.append(summary)
        return ApprovalDecision(self._approved, "U-approver")


class FakeHandle:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(
        self, channel: str, thread_ts: str, text: str, *, trigger: str = "human"
    ) -> None:
        self.calls.append((channel, thread_ts, text))


def _tool(store: MemoryStore, gate: FakeGate, now: float, name: str) -> Any:
    _server, _names, tools = build_schedule_mcp(
        store, gate, channel="C-team", thread_ts="1785.001", clock=lambda: now
    )
    return {t.name: t for t in tools}[name]


# A Monday, 10:00 in Jakarta -- so the next "Tuesday 09:00" is the following day.
_MONDAY_10AM = datetime(2026, 8, 3, 10, 0, tzinfo=ZoneInfo("Asia/Jakarta")).timestamp()


async def test_a_schedule_created_through_the_tool_fires_in_its_own_thread() -> None:
    store, gate, handle = MemoryStore(), FakeGate(approved=True), FakeHandle()

    result = await _tool(store, gate, _MONDAY_10AM, "create").handler(
        {
            "cron": "0 9 * * 2",
            "timezone": "Asia/Jakarta",
            "prompt": "post the weekly sprint summary",
        }
    )
    assert not result.get("is_error")
    assert len(gate.summaries) == 1

    row = (await store.list_schedules("C-team", "1785.001"))[0]
    due = datetime.fromtimestamp(row.next_run_at, ZoneInfo("Asia/Jakarta"))
    assert (due.weekday(), due.hour, due.minute) == (1, 9, 0)  # Tuesday 09:00 local

    # Nothing fires before it is due, even a minute out.
    early = ScheduleRunner(store, handle, clock=lambda: row.next_run_at - 60)
    assert await early.run_once() == 0
    assert handle.calls == []

    # At the due instant it fires, into the thread it was created in.
    runner = ScheduleRunner(store, handle, clock=lambda: row.next_run_at)
    assert await runner.run_once() == 1
    assert handle.calls == [("C-team", "1785.001", "post the weekly sprint summary")]

    # And it does not fire twice -- next_run_at advanced during the claim.
    assert await runner.run_once() == 0
    assert len(handle.calls) == 1

    # A week later it fires again, exactly once.
    a_week_on = ScheduleRunner(store, handle, clock=lambda: row.next_run_at + 7 * 86400)
    assert await a_week_on.run_once() == 1
    assert len(handle.calls) == 2


async def test_a_denied_schedule_never_fires() -> None:
    store, gate, handle = MemoryStore(), FakeGate(approved=False), FakeHandle()

    await _tool(store, gate, _MONDAY_10AM, "create").handler(
        {"cron": "0 9 * * 2", "timezone": "Asia/Jakarta", "prompt": "should never run"}
    )

    # Far past any plausible due time: nothing was written, so nothing fires.
    runner = ScheduleRunner(store, handle, clock=lambda: _MONDAY_10AM + 30 * 86400)
    assert await runner.run_once() == 0
    assert handle.calls == []


async def test_a_removed_schedule_stops_firing() -> None:
    store, gate, handle = MemoryStore(), FakeGate(approved=True), FakeHandle()

    await _tool(store, gate, _MONDAY_10AM, "create").handler(
        {"cron": "0 9 * * 2", "timezone": "Asia/Jakarta", "prompt": "weekly summary"}
    )
    row = (await store.list_schedules("C-team", "1785.001"))[0]

    runner = ScheduleRunner(store, handle, clock=lambda: row.next_run_at)
    assert await runner.run_once() == 1

    await _tool(store, gate, _MONDAY_10AM, "remove").handler({"id": row.id})

    later = ScheduleRunner(store, handle, clock=lambda: row.next_run_at + 30 * 86400)
    assert await later.run_once() == 0
    assert len(handle.calls) == 1

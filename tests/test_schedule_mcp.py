from __future__ import annotations

from typing import Any

from jean.db.memory import MemoryStore
from jean.ports import ApprovalDecision
from jean.schedule.mcp import build_schedule_mcp


class FakeGate:
    def __init__(self, approved: bool = True, by: str = "U9") -> None:
        self.requests: list[tuple[str, str, str]] = []
        self._decision = ApprovalDecision(approved, by)

    async def request(self, channel: str, thread_ts: str, summary: str) -> ApprovalDecision:
        self.requests.append((channel, thread_ts, summary))
        return self._decision


def _tools(store: MemoryStore, gate: FakeGate) -> dict[str, Any]:
    _server, _names, tools = build_schedule_mcp(
        store, gate, channel="C1", thread_ts="111.222", clock=lambda: 1000.0
    )
    return {t.name: t for t in tools}


def _text(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


async def test_create_asks_for_approval_and_writes_on_approve() -> None:
    store, gate = MemoryStore(), FakeGate(approved=True, by="U9")
    result = await _tools(store, gate)["create"].handler(
        {"cron": "0 9 * * 2", "timezone": "Asia/Jakarta", "prompt": "sprint summary"}
    )

    assert len(gate.requests) == 1
    rows = await store.list_schedules("C1", "111.222")
    assert len(rows) == 1
    assert rows[0].cron == "0 9 * * 2"
    # The approver is the accountable party, so that is who owns the row.
    assert rows[0].created_by == "U9"
    assert not result.get("is_error")


async def test_denied_creates_nothing() -> None:
    store, gate = MemoryStore(), FakeGate(approved=False)
    result = await _tools(store, gate)["create"].handler(
        {"cron": "0 9 * * 2", "timezone": "Asia/Jakarta", "prompt": "sprint summary"}
    )

    assert await store.list_schedules("C1", "111.222") == []
    assert result.get("is_error") is True


async def test_invalid_cron_never_reaches_the_approver() -> None:
    # Nobody should be asked to approve a schedule that cannot run.
    store, gate = MemoryStore(), FakeGate()
    result = await _tools(store, gate)["create"].handler(
        {"cron": "not a cron", "timezone": "UTC", "prompt": "p"}
    )

    assert gate.requests == []
    assert await store.list_schedules("C1", "111.222") == []
    assert result.get("is_error") is True


async def test_create_binds_the_calling_thread_not_its_arguments() -> None:
    store, gate = MemoryStore(), FakeGate()
    await _tools(store, gate)["create"].handler(
        {
            "cron": "0 9 * * 2",
            "timezone": "UTC",
            "prompt": "p",
            "channel": "C-EVIL",
            "thread_ts": "666.666",
        }
    )

    assert await store.list_schedules("C-EVIL", "666.666") == []
    assert len(await store.list_schedules("C1", "111.222")) == 1


async def test_list_shows_only_this_thread() -> None:
    store, gate = MemoryStore(), FakeGate()
    await store.create_schedule(
        channel="C1",
        thread_ts="111.222",
        cron="0 9 * * 2",
        timezone="UTC",
        prompt="mine",
        created_by="U1",
        next_run_at=1000.0,
    )
    await store.create_schedule(
        channel="C1",
        thread_ts="999.999",
        cron="0 9 * * 2",
        timezone="UTC",
        prompt="theirs",
        created_by="U1",
        next_run_at=1000.0,
    )

    text = _text(await _tools(store, gate)["list"].handler({}))

    assert "mine" in text
    assert "theirs" not in text


async def test_remove_asks_for_approval_and_deletes_on_approve() -> None:
    store, gate = MemoryStore(), FakeGate(approved=True)
    row = await store.create_schedule(
        channel="C1",
        thread_ts="111.222",
        cron="0 9 * * 2",
        timezone="UTC",
        prompt="p",
        created_by="U1",
        next_run_at=1000.0,
    )

    await _tools(store, gate)["remove"].handler({"id": row.id})

    assert len(gate.requests) == 1
    assert await store.list_schedules("C1", "111.222") == []


async def test_remove_denied_keeps_the_schedule() -> None:
    store, gate = MemoryStore(), FakeGate(approved=False)
    row = await store.create_schedule(
        channel="C1",
        thread_ts="111.222",
        cron="0 9 * * 2",
        timezone="UTC",
        prompt="p",
        created_by="U1",
        next_run_at=1000.0,
    )

    result = await _tools(store, gate)["remove"].handler({"id": row.id})

    assert len(await store.list_schedules("C1", "111.222")) == 1
    assert result.get("is_error") is True


async def test_remove_cannot_reach_another_threads_schedule() -> None:
    # An id from elsewhere is "not found" -- and must not even raise an approval,
    # which would leak that the id exists.
    store, gate = MemoryStore(), FakeGate(approved=True)
    row = await store.create_schedule(
        channel="C1",
        thread_ts="999.999",
        cron="0 9 * * 2",
        timezone="UTC",
        prompt="theirs",
        created_by="U1",
        next_run_at=1000.0,
    )

    result = await _tools(store, gate)["remove"].handler({"id": row.id})

    assert gate.requests == []
    assert result.get("is_error") is True
    assert len(await store.list_schedules("C1", "999.999")) == 1

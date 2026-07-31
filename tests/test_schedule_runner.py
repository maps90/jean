from __future__ import annotations

from jean.db.memory import MemoryStore
from jean.schedule.runner import ScheduleRunner


class FakeHandle:
    """Stands in for SessionManager.handle."""

    def __init__(self, raises: bool = False) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._raises = raises

    async def __call__(
        self, channel: str, thread_ts: str, text: str, *, trigger: str = "human"
    ) -> None:
        self.calls.append((channel, thread_ts, text))
        if self._raises:
            raise RuntimeError("turn blew up")


async def _make(store: MemoryStore, next_run_at: float) -> str:
    row = await store.create_schedule(
        channel="C1",
        thread_ts="111.222",
        cron="0 9 * * 2",
        timezone="UTC",
        prompt="post the weekly sprint summary",
        created_by="U1",
        next_run_at=next_run_at,
    )
    return row.id


async def test_fires_a_due_schedule_as_a_turn_in_its_thread() -> None:
    store = MemoryStore()
    await _make(store, 1000.0)
    handle = FakeHandle()
    runner = ScheduleRunner(store, handle, clock=lambda: 1000.0)

    assert await runner.run_once() == 1
    assert handle.calls == [("C1", "111.222", "post the weekly sprint summary")]


async def test_does_not_fire_before_it_is_due() -> None:
    store = MemoryStore()
    await _make(store, 1000.0)
    handle = FakeHandle()
    runner = ScheduleRunner(store, handle, clock=lambda: 999.0)

    assert await runner.run_once() == 0
    assert handle.calls == []


async def test_fires_late_inside_the_grace_window() -> None:
    store = MemoryStore()
    await _make(store, 1000.0)
    handle = FakeHandle()
    runner = ScheduleRunner(store, handle, grace_seconds=3600.0, clock=lambda: 1000.0 + 1800)

    assert await runner.run_once() == 1
    assert len(handle.calls) == 1


async def test_skips_and_marks_missed_past_the_grace_window() -> None:
    # A summary posted two days late carries a "weekly" framing that is no
    # longer true. Record the miss rather than posting it.
    store = MemoryStore()
    schedule_id = await _make(store, 1000.0)
    handle = FakeHandle()
    runner = ScheduleRunner(store, handle, grace_seconds=3600.0, clock=lambda: 1000.0 + 7200)

    assert await runner.run_once() == 0
    assert handle.calls == []
    row = (await store.list_schedules("C1", "111.222"))[0]
    assert row.id == schedule_id
    assert row.last_status == "missed"


async def test_advances_next_run_at_so_it_does_not_refire() -> None:
    store = MemoryStore()
    await _make(store, 1000.0)
    handle = FakeHandle()
    runner = ScheduleRunner(store, handle, clock=lambda: 1000.0)

    assert await runner.run_once() == 1
    assert await runner.run_once() == 0
    assert len(handle.calls) == 1


async def test_records_ok_after_a_successful_firing() -> None:
    store = MemoryStore()
    await _make(store, 1000.0)
    runner = ScheduleRunner(store, FakeHandle(), clock=lambda: 1000.0)

    await runner.run_once()

    row = (await store.list_schedules("C1", "111.222"))[0]
    assert row.last_status == "ok"
    assert row.last_run_at == 1000.0


async def test_a_failing_turn_is_recorded_and_does_not_stop_the_others() -> None:
    store = MemoryStore()
    await _make(store, 1000.0)
    runner = ScheduleRunner(store, FakeHandle(raises=True), clock=lambda: 1000.0)

    # run_once must not propagate: one bad schedule cannot kill the loop.
    assert await runner.run_once() == 0

    row = (await store.list_schedules("C1", "111.222"))[0]
    assert row.last_status == "error"


class _CountingMetrics:
    def __init__(self) -> None:
        self.schedule: list[str] = []

    def schedule_run(self, *, status: str) -> None:
        self.schedule.append(status)


async def test_runner_counts_each_firing_by_status():
    """ok / error / missed each have a different remediation -- a broken prompt,
    a broken agent, and a worker that was down through the window -- so the
    dashboard has to tell them apart."""
    store = MemoryStore()
    metrics = _CountingMetrics()
    await store.create_schedule(
        channel="C1",
        thread_ts="1.0",
        cron="* * * * *",
        timezone="UTC",
        prompt="ok one",
        created_by="U1",
        next_run_at=100.0,
    )
    await store.create_schedule(
        channel="C1",
        thread_ts="2.0",
        cron="* * * * *",
        timezone="UTC",
        prompt="boom",
        created_by="U1",
        next_run_at=100.0,
    )
    await store.create_schedule(
        channel="C1",
        thread_ts="3.0",
        cron="* * * * *",
        timezone="UTC",
        prompt="late",
        created_by="U1",
        next_run_at=0.0,  # far outside the grace window
    )

    async def handle(channel, thread_ts, prompt, *, trigger="human"):
        if prompt == "boom":
            raise RuntimeError("turn failed")

    runner = ScheduleRunner(store, handle, grace_seconds=60.0, clock=lambda: 100.0, metrics=metrics)
    await runner.run_once()

    assert sorted(metrics.schedule) == ["error", "missed", "ok"]


async def test_runner_labels_its_turns_as_schedule_triggered():
    """Token spend has to be attributable to cron rather than to people."""
    store = MemoryStore()
    seen: list[str] = []

    await store.create_schedule(
        channel="C1",
        thread_ts="1.0",
        cron="* * * * *",
        timezone="UTC",
        prompt="hello",
        created_by="U1",
        next_run_at=100.0,
    )

    async def handle(channel, thread_ts, prompt, *, trigger="human"):
        seen.append(trigger)

    runner = ScheduleRunner(store, handle, clock=lambda: 100.0)
    await runner.run_once()

    assert seen == ["schedule"]

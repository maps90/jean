"""Shared behavioral assertions for anything implementing SessionStore +
ApprovalCoordinator + ThreadLock (MemoryStore, PostgresStore). Both adapters
must satisfy every function here identically -- that's what proves them
equivalent.
"""

from __future__ import annotations

import asyncio


async def assert_session_roundtrip_and_engagement(store) -> None:
    channel, thread_ts = "C1", "111.222"
    assert await store.get_session(channel, thread_ts) is None
    assert await store.is_engaged(channel, thread_ts) is False

    await store.upsert_session(channel, thread_ts, sdk_session_id="sdk-abc")
    row = await store.get_session(channel, thread_ts)
    assert row is not None
    assert row.channel == channel
    assert row.thread_ts == thread_ts
    assert row.sdk_session_id == "sdk-abc"
    assert row.engaged is False
    assert row.last_active_at > 0

    await store.set_engaged(channel, thread_ts, True)
    assert await store.is_engaged(channel, thread_ts) is True
    row = await store.get_session(channel, thread_ts)
    assert row.engaged is True
    # sdk_session_id must survive an update that doesn't touch it.
    assert row.sdk_session_id == "sdk-abc"

    await store.upsert_session(channel, thread_ts, permission_mode="plan", touch=False)
    row = await store.get_session(channel, thread_ts)
    assert row.permission_mode == "plan"
    assert row.sdk_session_id == "sdk-abc"
    assert row.engaged is True


async def assert_thread_lock_serializes_same_thread(lock) -> None:
    events: list[str] = []
    barrier_entered = asyncio.Event()

    async def first():
        async with lock("C1", "T1"):
            events.append("first-start")
            barrier_entered.set()
            await asyncio.sleep(0.05)
            events.append("first-end")

    async def second():
        await barrier_entered.wait()
        async with lock("C1", "T1"):
            events.append("second-start")

    await asyncio.gather(first(), second())
    assert events == ["first-start", "first-end", "second-start"]


async def assert_thread_lock_allows_different_threads(lock) -> None:
    events: list[str] = []

    async def hold(name, thread_ts, delay):
        async with lock("C1", thread_ts):
            events.append(f"{name}-start")
            await asyncio.sleep(delay)
            events.append(f"{name}-end")

    await asyncio.gather(hold("a", "T1", 0.05), hold("b", "T2", 0.01))
    # b (shorter sleep, different thread) finishes before a, proving no cross-thread block.
    assert events.index("b-end") < events.index("a-end")


async def assert_coordinator_approve_flow(coordinator) -> None:
    await coordinator.create("appr-1", "C1", "111.0", "do the thing")

    async def approver():
        await asyncio.sleep(0.02)
        resolved = await coordinator.resolve("appr-1", True, "U123")
        assert resolved is True

    waiter = asyncio.ensure_future(coordinator.wait("appr-1", timeout=5))
    await approver()
    decision = await waiter
    assert decision.approved is True
    assert decision.by == "U123"


async def assert_coordinator_timeout_denies(coordinator) -> None:
    await coordinator.create("appr-timeout", "C1", "111.0", "do the risky thing")
    decision = await coordinator.wait("appr-timeout", timeout=0.05)
    assert decision.approved is False
    assert decision.by == "system"


async def assert_coordinator_resolve_unknown_returns_false(coordinator) -> None:
    resolved = await coordinator.resolve("nope", True, "U123")
    assert resolved is False


async def assert_coordinator_wait_unknown_id_denies_after_timeout(coordinator) -> None:
    """wait() on an id nobody ever create()d must behave like any other
    timeout: block for ~`timeout`, then a system-deny. Both adapters must
    agree here -- Memory used to return instantly while Postgres blocked."""
    import time

    start = time.monotonic()
    decision = await coordinator.wait("never-created", timeout=0.05)
    elapsed = time.monotonic() - start
    assert decision.approved is False
    assert decision.by == "system"
    assert elapsed >= 0.05


async def assert_coordinator_stores_approvers_and_pending(coordinator) -> None:
    await coordinator.create("appr-approvers", "C9", "999.0", "deploy the thing")
    assert await coordinator.approvers_of("appr-approvers") == set()

    await coordinator.set_approvers("appr-approvers", {"U1", "U2"})
    assert await coordinator.approvers_of("appr-approvers") == {"U1", "U2"}

    pending = await coordinator.get_pending("appr-approvers")
    assert pending == ("C9", "999.0", "deploy the thing")

    assert await coordinator.get_pending("missing-id") is None

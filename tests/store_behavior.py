"""Shared behavioral assertions for anything implementing SessionStore +
ApprovalCoordinator + ThreadLock (MemoryStore, PostgresStore). Both adapters
must satisfy every function here identically -- that's what proves them
equivalent.
"""

from __future__ import annotations

import asyncio
import time

import pytest


async def assert_session_roundtrip(store) -> None:
    channel, thread_ts = "C1", "111.222"
    assert await store.get_session(channel, thread_ts) is None

    await store.upsert_session(channel, thread_ts, sdk_session_id="sdk-abc")
    row = await store.get_session(channel, thread_ts)
    assert row is not None
    assert row.channel == channel
    assert row.thread_ts == thread_ts
    assert row.sdk_session_id == "sdk-abc"
    assert row.last_active_at > 0

    await store.upsert_session(channel, thread_ts, permission_mode="plan", touch=False)
    row = await store.get_session(channel, thread_ts)
    assert row.permission_mode == "plan"
    # sdk_session_id must survive an update that doesn't touch it.
    assert row.sdk_session_id == "sdk-abc"


async def assert_partner_roundtrip(store) -> None:
    """One conversation partner per thread. `None` means nobody -- and clearing
    to `None` must be distinguishable from 'leave it alone', which is why this
    is a dedicated setter rather than a field on upsert_session()."""
    channel, thread_ts = "C9", "999.111"
    assert await store.get_partner(channel, thread_ts) is None

    await store.set_partner(channel, thread_ts, "U11111")
    assert await store.get_partner(channel, thread_ts) == "U11111"
    row = await store.get_session(channel, thread_ts)
    assert row is not None
    assert row.engaged_with == "U11111"

    # A second mention hands the conversation to someone else.
    await store.set_partner(channel, thread_ts, "U22222")
    assert await store.get_partner(channel, thread_ts) == "U22222"

    # Clearing to None is a real, storable state, not a no-op.
    await store.set_partner(channel, thread_ts, None)
    assert await store.get_partner(channel, thread_ts) is None

    # The partner must survive an unrelated update that doesn't mention it.
    await store.set_partner(channel, thread_ts, "U11111")
    await store.upsert_session(channel, thread_ts, sdk_session_id="sdk-xyz", touch=False)
    assert await store.get_partner(channel, thread_ts) == "U11111"


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


async def assert_prune_removes_resolved_approvals_and_stale_sessions(store) -> None:
    """prune(sessions_older_than=, approvals_older_than=) deletes resolved
    approvals and sessions last active before their respective cutoffs,
    leaving pending approvals untouched. Each cutoff is an absolute epoch
    value -- a row is stale iff its timestamp is < cutoff."""
    # A resolved approval and a touched session both timestamped ~now.
    await store.create("appr-old", "C1", "1.0", "old work")
    await store.resolve("appr-old", True, "U1")
    await store.upsert_session("C1", "1.0", sdk_session_id="sdk-old")
    # A still-pending approval -- must never be pruned regardless of age.
    await store.create("appr-pending", "C2", "2.0", "awaiting a human")

    # Cutoffs in the far future make every timestamped row look stale.
    result = await store.prune(
        sessions_older_than=time.time() + 1000, approvals_older_than=time.time() + 1000
    )
    assert result.approvals_deleted == 1
    assert result.sessions_deleted == 1
    # Resolved approval + stale session gone; pending approval survives.
    assert await store.get_pending("appr-old") is None
    assert await store.get_session("C1", "1.0") is None
    assert await store.get_pending("appr-pending") == ("C2", "2.0", "awaiting a human")


async def assert_prune_keeps_recent_rows(store) -> None:
    """Cutoffs in the past delete nothing -- recent rows survive."""
    await store.create("appr-fresh", "C1", "1.0", "fresh work")
    await store.resolve("appr-fresh", True, "U1")
    await store.upsert_session("C1", "1.0", sdk_session_id="sdk-fresh")

    result = await store.prune(
        sessions_older_than=time.time() - 1000, approvals_older_than=time.time() - 1000
    )
    assert result.approvals_deleted == 0
    assert result.sessions_deleted == 0
    assert await store.get_pending("appr-fresh") == ("C1", "1.0", "fresh work")
    assert await store.get_session("C1", "1.0") is not None


async def assert_try_claim_cleanup_gates_on_interval(store) -> None:
    """The first claim wins; a second claim within the interval is refused;
    once the interval has elapsed (min_interval=0), a claim wins again. This
    is what makes exactly one worker prune per period."""
    assert await store.try_claim_cleanup(min_interval=1000) is True
    # Just claimed -- not due again for another ~1000s.
    assert await store.try_claim_cleanup(min_interval=1000) is False
    # A zero interval is always due, so the next claim succeeds.
    assert await store.try_claim_cleanup(min_interval=0) is True


async def assert_turn_seq_increments(store) -> None:
    channel, thread_ts = "C-seq", "900.1"
    await store.upsert_session(channel, thread_ts, sdk_session_id="sdk-1")
    assert (await store.get_session(channel, thread_ts)).turn_seq == 0

    assert await store.bump_turn(channel, thread_ts) == 1
    assert await store.bump_turn(channel, thread_ts) == 2
    assert (await store.get_session(channel, thread_ts)).turn_seq == 2


async def assert_transcript_roundtrip(store) -> None:
    channel, thread_ts = "C-tr", "901.1"
    await store.upsert_session(channel, thread_ts, sdk_session_id="sid-a")

    assert await store.load(channel, thread_ts, "sid-a") is None

    blob = b'{"type":"user","sessionId":"sid-a"}\n' * 50
    await store.save(channel, thread_ts, "sid-a", blob)
    assert await store.load(channel, thread_ts, "sid-a") == blob

    # A transcript stored under a different session id must never be handed back:
    # resuming with the wrong transcript would corrupt the thread's memory.
    assert await store.load(channel, thread_ts, "sid-b") is None

    # The newest turn's transcript replaces the previous one.
    bigger = blob + b'{"type":"assistant"}\n'
    await store.save(channel, thread_ts, "sid-a", bigger)
    assert await store.load(channel, thread_ts, "sid-a") == bigger


async def assert_prune_uses_separate_windows_and_drops_transcripts(store) -> None:
    now = time.time()
    old, recent = now - 10 * 86400, now - 1 * 86400

    # A session idle 10 days, with a transcript.
    await store.upsert_session("C-old", "1.0", sdk_session_id="sid-old")
    await store.save("C-old", "1.0", "sid-old", b"stale bytes")
    # A session idle 1 day.
    await store.upsert_session("C-new", "2.0", sdk_session_id="sid-new")

    await _backdate_session(store, "C-old", "1.0", old)
    await _backdate_session(store, "C-new", "2.0", recent)

    # Sessions expire at 3 days; approvals at 30. The 10-day session goes; the
    # 1-day one stays. A single shared window could not express this.
    result = await store.prune(
        sessions_older_than=now - 3 * 86400,
        approvals_older_than=now - 30 * 86400,
    )

    assert result.sessions_deleted == 1
    assert await store.get_session("C-old", "1.0") is None
    assert await store.get_session("C-new", "2.0") is not None
    # the transcript went with its session row
    assert await store.load("C-old", "1.0", "sid-old") is None


async def assert_bump_turn_on_a_new_thread_is_not_born_expired(store) -> None:
    """bump_turn's create-the-row-if-absent branch must not leave last_active_at
    at its zero default -- a row born that way would already be older than any
    retention cutoff, so the very next cleanup sweep would delete it."""
    channel, thread_ts = "C-newborn", "1000.1"
    assert await store.get_session(channel, thread_ts) is None

    assert await store.bump_turn(channel, thread_ts) == 1
    row = await store.get_session(channel, thread_ts)
    assert row is not None
    assert row.last_active_at > 0

    result = await store.prune(
        sessions_older_than=time.time() - 3600, approvals_older_than=time.time() - 3600
    )
    assert result.sessions_deleted == 0
    assert await store.get_session(channel, thread_ts) is not None


async def assert_save_requires_an_existing_session(store) -> None:
    """save() must mirror the Postgres FK: a transcript cannot exist without
    its session row. The two adapters raise different exception TYPES --
    Postgres raises asyncpg's ForeignKeyViolationError, MemoryStore raises a
    built-in -- so this only pins the *behavior* (save() raises for an
    unknown thread), not the type. After upsert_session(...), the same save()
    call must succeed and round-trip normally."""
    channel, thread_ts = "C-fk", "902.1"
    assert await store.get_session(channel, thread_ts) is None

    with pytest.raises(Exception):  # noqa: B017 -- type intentionally unpinned, see docstring
        await store.save(channel, thread_ts, "sid-fk", b"payload")

    await store.upsert_session(channel, thread_ts, sdk_session_id="sid-fk")
    await store.save(channel, thread_ts, "sid-fk", b"payload")
    assert await store.load(channel, thread_ts, "sid-fk") == b"payload"


async def _backdate_session(store, channel: str, thread_ts: str, when: float) -> None:
    """Force a session's last_active_at into the past. Both adapters store it as
    an epoch float, but only through their own writes -- so reach in per adapter."""
    if hasattr(store, "_sessions"):  # MemoryStore
        store._sessions[(channel, thread_ts)].last_active_at = when
    else:  # PostgresStore
        await store._pool.execute(
            "UPDATE sessions SET last_active_at=$3 WHERE channel=$1 AND thread_ts=$2",
            channel,
            thread_ts,
            when,
        )

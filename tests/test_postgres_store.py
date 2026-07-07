from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from jean.db.postgres import PostgresStore

pytestmark = pytest.mark.skipif(not os.environ.get("JEAN_TEST_DATABASE_URL"), reason="no test db")

from tests.store_behavior import (  # noqa: E402
    assert_coordinator_approve_flow,
    assert_coordinator_resolve_unknown_returns_false,
    assert_coordinator_stores_approvers_and_pending,
    assert_coordinator_timeout_denies,
    assert_coordinator_wait_unknown_id_denies_after_timeout,
    assert_session_roundtrip_and_engagement,
    assert_thread_lock_allows_different_threads,
    assert_thread_lock_serializes_same_thread,
)


@pytest.fixture
async def store():
    dsn = os.environ["JEAN_TEST_DATABASE_URL"]
    s = await PostgresStore.connect(dsn)
    async with s._pool.acquire() as c:
        await c.execute("TRUNCATE sessions, approvals")
    yield s
    await s.close()


def _uid() -> str:
    return uuid.uuid4().hex[:8]


async def test_ping(store):
    assert await store.ping() is True


async def test_session_roundtrip_and_engagement(store):
    await assert_session_roundtrip_and_engagement(store)


async def test_thread_lock_serializes_same_thread(store):
    await assert_thread_lock_serializes_same_thread(store)


async def test_thread_lock_allows_different_threads(store):
    await assert_thread_lock_allows_different_threads(store)


async def test_coordinator_approve_flow(store):
    await assert_coordinator_approve_flow(store)


async def test_coordinator_timeout_denies(store):
    await assert_coordinator_timeout_denies(store)


async def test_coordinator_resolve_unknown_returns_false(store):
    await assert_coordinator_resolve_unknown_returns_false(store)


async def test_coordinator_stores_approvers_and_pending(store):
    await assert_coordinator_stores_approvers_and_pending(store)


async def test_coordinator_wait_unknown_id_denies_after_timeout(store):
    await assert_coordinator_wait_unknown_id_denies_after_timeout(store)


async def test_notify_wakes_a_different_connection(store):
    """The core cross-worker guarantee: wait() on one logical caller is woken
    by resolve() issued concurrently -- exercised over the pool so the LISTEN
    and the NOTIFY can land on different physical connections."""
    aid = f"appr-notify-{_uid()}"
    await store.create(aid, "C1", "111.0", "cross-conn notify")

    async def resolver():
        await asyncio.sleep(0.05)
        assert await store.resolve(aid, True, "U999") is True

    decision, _ = await asyncio.gather(store.wait(aid, timeout=5), resolver())
    assert decision.approved is True
    assert decision.by == "U999"


async def test_locks_do_not_starve_pool_or_approval_wait(store):
    """Regression for the C1 pool-exhaustion deadlock: the advisory-lock
    connection and the wait() LISTEN connection must be dedicated
    (asyncpg.connect), not drawn from the shared query pool (max_size=10).
    Hold more concurrent locks than the pool size and prove a normal
    wait()/resolve() round-trip (and a short query) still completes quickly
    instead of hanging behind the held locks."""
    n_locks = 12  # > pool max_size=10
    release = asyncio.Event()
    entered = [asyncio.Event() for _ in range(n_locks)]

    async def hold_lock(i: int):
        async with store(f"C{i}", f"lock-thread-{i}"):
            entered[i].set()
            await release.wait()

    lock_tasks = [asyncio.ensure_future(hold_lock(i)) for i in range(n_locks)]
    try:
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in entered)), timeout=5)

        # A short query must still complete promptly while all locks are held.
        assert await asyncio.wait_for(store.is_engaged("C1", "111.0"), timeout=2) is False

        aid = f"appr-starve-{_uid()}"
        await store.create(aid, "C1", "111.0", "should not starve")
        await store.set_approvers(aid, {"U1"})

        async def resolver():
            await asyncio.sleep(0.05)
            assert await store.resolve(aid, True, "U1") is True

        decision, _ = await asyncio.wait_for(
            asyncio.gather(store.wait(aid, timeout=5), resolver()), timeout=5
        )
        assert decision.approved is True
        assert decision.by == "U1"
    finally:
        release.set()
        await asyncio.gather(*lock_tasks)

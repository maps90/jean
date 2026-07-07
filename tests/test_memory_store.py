from __future__ import annotations

import pytest

from jean.db.memory import MemoryStore
from tests.store_behavior import (
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
def store():
    return MemoryStore()


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

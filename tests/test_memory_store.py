from __future__ import annotations

import pytest

from jean.db.memory import MemoryStore
from tests.store_behavior import (
    assert_bump_turn_on_a_new_thread_is_not_born_expired,
    assert_coordinator_approve_flow,
    assert_coordinator_resolve_unknown_returns_false,
    assert_coordinator_stores_approvers_and_pending,
    assert_coordinator_timeout_denies,
    assert_coordinator_wait_unknown_id_denies_after_timeout,
    assert_partner_roundtrip,
    assert_prune_keeps_recent_rows,
    assert_prune_removes_resolved_approvals_and_stale_sessions,
    assert_prune_uses_separate_windows_and_drops_transcripts,
    assert_save_requires_an_existing_session,
    assert_session_roundtrip_and_engagement,
    assert_thread_lock_allows_different_threads,
    assert_thread_lock_serializes_same_thread,
    assert_transcript_roundtrip,
    assert_try_claim_cleanup_gates_on_interval,
    assert_turn_seq_increments,
)


@pytest.fixture
def store():
    return MemoryStore()


async def test_session_roundtrip_and_engagement(store):
    await assert_session_roundtrip_and_engagement(store)


async def test_partner_roundtrip(store):
    await assert_partner_roundtrip(store)


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


async def test_prune_removes_resolved_approvals_and_stale_sessions(store):
    await assert_prune_removes_resolved_approvals_and_stale_sessions(store)


async def test_prune_keeps_recent_rows(store):
    await assert_prune_keeps_recent_rows(store)


async def test_try_claim_cleanup_gates_on_interval(store):
    await assert_try_claim_cleanup_gates_on_interval(store)


async def test_turn_seq_increments(store):
    await assert_turn_seq_increments(store)


async def test_transcript_roundtrip(store):
    await assert_transcript_roundtrip(store)


async def test_prune_uses_separate_windows_and_drops_transcripts(store):
    await assert_prune_uses_separate_windows_and_drops_transcripts(store)


async def test_bump_turn_on_a_new_thread_is_not_born_expired(store):
    await assert_bump_turn_on_a_new_thread_is_not_born_expired(store)


async def test_save_requires_an_existing_session(store):
    await assert_save_requires_an_existing_session(store)

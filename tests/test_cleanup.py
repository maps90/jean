from __future__ import annotations

from jean.maintenance.cleanup import CleanupScheduler
from jean.ports import PruneResult


class FakeStore:
    """Records prune calls and lets a test dictate the claim outcome."""

    def __init__(self, *, claim: bool) -> None:
        self._claim = claim
        self.claim_calls: list[float] = []
        self.prune_cutoffs: list[tuple[float, float]] = []

    async def try_claim_cleanup(self, min_interval: float) -> bool:
        self.claim_calls.append(min_interval)
        return self._claim

    async def prune(
        self, *, sessions_older_than: float, approvals_older_than: float
    ) -> PruneResult:
        self.prune_cutoffs.append((sessions_older_than, approvals_older_than))
        return PruneResult(approvals_deleted=2, sessions_deleted=3)


async def test_run_once_prunes_at_cutoff_when_claim_won():
    store = FakeStore(claim=True)
    # Fixed clock so the cutoff is deterministic: now (1_000_000) - 7 days.
    scheduler = CleanupScheduler(
        store,
        session_retention_seconds=7 * 86400,
        approval_retention_seconds=7 * 86400,
        interval_seconds=7 * 86400,
        clock=lambda: 1_000_000.0,
    )

    result = await scheduler.run_once()

    assert store.claim_calls == [7 * 86400]
    cutoff = 1_000_000.0 - 7 * 86400
    assert store.prune_cutoffs == [(cutoff, cutoff)]
    assert result == PruneResult(approvals_deleted=2, sessions_deleted=3)


async def test_run_once_skips_prune_when_claim_lost():
    store = FakeStore(claim=False)
    scheduler = CleanupScheduler(
        store,
        session_retention_seconds=7 * 86400,
        approval_retention_seconds=7 * 86400,
        interval_seconds=7 * 86400,
    )

    result = await scheduler.run_once()

    assert store.claim_calls == [7 * 86400]
    assert store.prune_cutoffs == []  # another worker owns this cycle
    assert result is None


async def test_sessions_and_approvals_expire_on_separate_schedules():
    """A thread's memory going stale (3 days) and an audit record aging out
    (30 days) are different concerns -- one window cannot express both."""
    seen: list[dict] = []

    class RecordingStore:
        async def try_claim_cleanup(self, min_interval: float) -> bool:
            return True

        async def prune(self, *, sessions_older_than: float, approvals_older_than: float):
            seen.append({"sessions": sessions_older_than, "approvals": approvals_older_than})
            return PruneResult(approvals_deleted=0, sessions_deleted=0)

    scheduler = CleanupScheduler(
        RecordingStore(),
        session_retention_seconds=3 * 86400,
        approval_retention_seconds=30 * 86400,
        clock=lambda: 1_000_000.0,
    )
    await scheduler.run_once()

    assert seen == [{"sessions": 1_000_000.0 - 3 * 86400, "approvals": 1_000_000.0 - 30 * 86400}]


def test_cleanup_interval_defaults_to_daily():
    """A 3-day retention window swept weekly would let rows live ~10 days."""
    scheduler = CleanupScheduler(
        object(), session_retention_seconds=1, approval_retention_seconds=1
    )
    assert scheduler._interval_seconds == 86400

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from jean.ports import MaintenanceStore, PruneResult

logger = logging.getLogger("jean.maintenance")

_DAY_SECONDS = 86400


class CleanupScheduler:
    """Periodically prunes stale rows so Postgres doesn't grow without bound.

    Runs on every worker but claims the work through the store's
    `try_claim_cleanup` gate, so across N stateless workers exactly one prune
    happens per `interval_seconds`. Sessions and approvals expire on separate
    windows -- a thread's memory going stale and an audit record aging out are
    different concerns. The loop is resilient: a failed cycle is logged and
    retried on the next check rather than killing the task.
    """

    def __init__(
        self,
        store: MaintenanceStore,
        *,
        session_retention_seconds: float,
        approval_retention_seconds: float,
        interval_seconds: float = _DAY_SECONDS,
        check_seconds: float = 3600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._session_retention_seconds = session_retention_seconds
        self._approval_retention_seconds = approval_retention_seconds
        self._interval_seconds = interval_seconds
        self._check_seconds = check_seconds
        self._clock = clock

    async def run_once(self) -> PruneResult | None:
        """Claim the cycle and prune if we won; return None if a peer owns it."""
        if not await self._store.try_claim_cleanup(self._interval_seconds):
            return None
        now = self._clock()
        result = await self._store.prune(
            sessions_older_than=now - self._session_retention_seconds,
            approvals_older_than=now - self._approval_retention_seconds,
        )
        logger.info(
            "retention cleanup pruned %d approvals (>%.0fs) and %d sessions with their "
            "transcripts (>%.0fs)",
            result.approvals_deleted,
            self._approval_retention_seconds,
            result.sessions_deleted,
            self._session_retention_seconds,
        )
        return result

    async def run(self) -> None:
        """Background loop: check every `check_seconds`, prune when due."""
        while True:
            await asyncio.sleep(self._check_seconds)
            try:
                await self.run_once()
            except Exception:
                # A cleanup failure must not take down the loop; log and retry
                # on the next check.
                logger.exception("retention cleanup cycle failed")

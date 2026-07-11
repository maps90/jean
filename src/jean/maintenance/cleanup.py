from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from jean.ports import MaintenanceStore, PruneResult

logger = logging.getLogger("jean.maintenance")

_WEEK_SECONDS = 7 * 86400


class CleanupScheduler:
    """Periodically prunes stale rows so Postgres doesn't grow without bound.

    Runs on every worker but claims the work through the store's
    `try_claim_cleanup` gate, so across N stateless workers exactly one prune
    happens per `interval_seconds`. Each run deletes rows older than
    `retention_seconds`. The loop is resilient: a failed cycle is logged and
    retried on the next check rather than killing the task.
    """

    def __init__(
        self,
        store: MaintenanceStore,
        *,
        retention_seconds: float,
        interval_seconds: float = _WEEK_SECONDS,
        check_seconds: float = 3600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._retention_seconds = retention_seconds
        self._interval_seconds = interval_seconds
        self._check_seconds = check_seconds
        self._clock = clock

    async def run_once(self) -> PruneResult | None:
        """Claim the cycle and prune if we won; return None if a peer owns it."""
        if not await self._store.try_claim_cleanup(self._interval_seconds):
            return None
        cutoff = self._clock() - self._retention_seconds
        result = await self._store.prune(cutoff)
        logger.info(
            "retention cleanup pruned %d approvals, %d sessions (older than %.0fs)",
            result.approvals_deleted,
            result.sessions_deleted,
            self._retention_seconds,
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

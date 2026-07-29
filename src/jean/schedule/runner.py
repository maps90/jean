from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from jean.ports import Schedule, ScheduleStore
from jean.schedule.cron import next_after

logger = logging.getLogger("jean.schedule")

Handle = Callable[[str, str, str], Awaitable[None]]


class ScheduleRunner:
    """Fires due schedules as ordinary turns in their own threads.

    Injection happens at SessionManager.handle(), below the gateway: handle()
    already takes the per-thread lock, so cross-worker serialization is inherited
    rather than rebuilt, and engagement -- which lives in the gateway above it --
    is left untouched. A scheduled post therefore never cuts into a live exchange
    and never makes the agent believe a conversation just started.
    """

    def __init__(
        self,
        store: ScheduleStore,
        handle: Handle,
        *,
        grace_seconds: float = 3600.0,
        poll_seconds: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._handle = handle
        self._grace_seconds = grace_seconds
        self._poll_seconds = poll_seconds
        self._clock = clock

    def _advance(self, schedule: Schedule) -> float:
        return next_after(schedule.cron, schedule.timezone, schedule.next_run_at)

    async def run_once(self) -> int:
        """Claim everything due and fire it. Returns how many actually ran."""
        now = self._clock()
        claimed = await self._store.claim_due_schedules(now, self._advance)
        fired = 0
        for schedule in claimed:
            late = now - schedule.next_run_at
            if late > self._grace_seconds:
                logger.warning(
                    "schedule %s missed by %.0fs (grace %.0fs) -- skipping to next occurrence",
                    schedule.id,
                    late,
                    self._grace_seconds,
                )
                await self._store.record_run(schedule.id, last_run_at=now, last_status="missed")
                continue
            try:
                await self._handle(schedule.channel, schedule.thread_ts, schedule.prompt)
            except Exception:
                # One bad schedule must not stop the rest, nor kill the loop.
                # next_run_at already advanced, so this retries at the next
                # occurrence rather than immediately.
                logger.exception("schedule %s failed", schedule.id)
                await self._store.record_run(schedule.id, last_run_at=now, last_status="error")
                continue
            await self._store.record_run(schedule.id, last_run_at=now, last_status="ok")
            fired += 1
        return fired

    async def run(self) -> None:
        """Background loop: poll, fire what is due, never die."""
        while True:
            await asyncio.sleep(self._poll_seconds)
            try:
                await self.run_once()
            except Exception:
                logger.exception("schedule poll failed")

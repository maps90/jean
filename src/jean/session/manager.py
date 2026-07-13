from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from jean.ports import ThreadLock


class SessionManager:
    """Per-worker cache of JeanSession, keyed by (channel, thread_ts), plus
    cross-worker turn serialization via the ThreadLock port.

    The cache is best-effort: it exists to avoid rebuilding/reconnecting a
    session for every message, not for correctness. If an entry is dropped
    (idle sweep, worker restart), the next `handle()` call just rebuilds it
    and the underlying JeanSession resumes from the SessionStore (the
    stateless-worker scaling model).
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[str, str], Any],
        lock: ThreadLock,
        idle_seconds: float,
    ) -> None:
        self._session_factory = session_factory
        self._lock = lock
        self._idle_seconds = idle_seconds
        self._cache: dict[tuple[str, str], Any] = {}
        self._last_touch: dict[tuple[str, str], float] = {}

    async def handle(self, channel: str, thread_ts: str, text: str) -> None:
        key = (channel, thread_ts)
        async with self._lock(channel, thread_ts):
            session = self._cache.get(key)
            if session is None:
                session = self._session_factory(channel, thread_ts)
                self._cache[key] = session
            self._last_touch[key] = time.time()
            await session.run_turn(text)
            # Re-stamp AFTER the turn, not just before it: a turn that parked on a
            # human approval can run for longer than idle_seconds, and an entry-only
            # stamp would leave the session already idle the moment it finishes --
            # swept before it ever gets to answer another message.
            self._last_touch[key] = time.time()

    def _is_idle(self, key: tuple[str, str], now: float) -> bool:
        touched = self._last_touch.get(key)
        return touched is not None and now - touched > self._idle_seconds

    async def sweep(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        idle_keys = [key for key in list(self._last_touch) if self._is_idle(key, now)]
        for key in idle_keys:
            channel, thread_ts = key
            session = self._cache.get(key)
            # Never sweep a session whose turn is in flight. `_last_touch` is stamped
            # before the turn, and a turn parked on a human approval outlives the idle
            # window (approval_ttl 30min > idle_minutes 15) -- so "idle" here does not
            # mean "not running". close() tears the client down and deletes the .jsonl
            # the CLI child still has open: the turn would then archive nothing and the
            # thread would silently rewind to its last archived turn. Leave it cached;
            # a later sweep takes it once it is done.
            #
            # Checked BEFORE taking the lock, deliberately: a busy session holds the
            # thread lock for its whole turn, so blocking on it here would park the
            # sweeper behind a 30-minute human approval.
            if session is not None and session.busy:
                continue
            # Pop AND close under the per-thread lock. close() deletes this thread's
            # local .jsonl, but only after awaiting the client's teardown -- which
            # SIGTERMs the CLI child and yields the event loop for hundreds of ms.
            # Outside the lock, the user's next message (the "first message after a
            # lull" -- precisely when the sweeper fires) would find the cache already
            # popped, build a fresh session, hydrate that same .jsonl from the store
            # and resume on it, and the late delete would land on the file the live
            # turn is running on: either the resume fails and a stub transcript is
            # archived over the thread's only durable history, or the CLI holds the
            # unlinked inode and the thread silently stops replicating to Postgres.
            # The sweeper is a background task, so waiting here costs nothing, and a
            # busy session was already skipped -- the lock is almost never contended.
            async with self._lock(channel, thread_ts):
                # A turn may have run while we waited for the lock. Re-check against
                # the same `now`, and leave a session that is no longer idle cached:
                # closing it would throw away the client that turn just connected.
                if not self._is_idle(key, now):
                    continue
                session = self._cache.pop(key, None)
                self._last_touch.pop(key, None)
                if session is not None:
                    await session.close()

    async def run_sweeper(self, interval: float = 60) -> None:
        while True:
            await asyncio.sleep(interval)
            await self.sweep()

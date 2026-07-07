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

    async def sweep(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        idle_keys = [
            key for key, touched in self._last_touch.items() if now - touched > self._idle_seconds
        ]
        for key in idle_keys:
            session = self._cache.pop(key, None)
            self._last_touch.pop(key, None)
            if session is not None:
                await session.close()

    async def run_sweeper(self, interval: float = 60) -> None:
        while True:
            await asyncio.sleep(interval)
            await self.sweep()

from __future__ import annotations

import asyncio
import time

from jean.db.memory import MemoryStore
from jean.session.manager import SessionManager


class FakeSession:
    def __init__(self, channel: str, thread_ts: str):
        self.channel = channel
        self.thread_ts = thread_ts
        self.turns: list[str] = []
        self.closed = False

    async def run_turn(self, text: str) -> None:
        self.turns.append(text)

    async def close(self) -> None:
        self.closed = True


def _factory(created: list[FakeSession]):
    def factory(channel: str, thread_ts: str) -> FakeSession:
        session = FakeSession(channel, thread_ts)
        created.append(session)
        return session

    return factory


async def test_handle_reuses_the_same_session_for_a_thread():
    created: list[FakeSession] = []
    manager = SessionManager(session_factory=_factory(created), lock=MemoryStore(), idle_seconds=60)

    await manager.handle("C1", "111.0", "one")
    await manager.handle("C1", "111.0", "two")

    assert len(created) == 1
    assert created[0].turns == ["one", "two"]


async def test_handle_uses_separate_sessions_per_thread():
    created: list[FakeSession] = []
    manager = SessionManager(session_factory=_factory(created), lock=MemoryStore(), idle_seconds=60)

    await manager.handle("C1", "111.0", "one")
    await manager.handle("C1", "222.0", "two")

    assert len(created) == 2


async def test_thread_lock_serializes_turns_on_the_same_thread():
    order: list[str] = []

    class SlowSession(FakeSession):
        async def run_turn(self, text: str) -> None:
            order.append(f"{text}-start")
            await asyncio.sleep(0.03)
            order.append(f"{text}-end")

    created: list[FakeSession] = []

    def factory(channel, thread_ts):
        s = SlowSession(channel, thread_ts)
        created.append(s)
        return s

    manager = SessionManager(session_factory=factory, lock=MemoryStore(), idle_seconds=60)

    await asyncio.gather(
        manager.handle("C1", "111.0", "a"),
        manager.handle("C1", "111.0", "b"),
    )

    # whichever turn ran first must fully finish before the other starts --
    # no interleaving of "start"/"end" across the two turns.
    assert order in (
        ["a-start", "a-end", "b-start", "b-end"],
        ["b-start", "b-end", "a-start", "a-end"],
    )


async def test_sweep_closes_and_drops_idle_sessions():
    created: list[FakeSession] = []
    manager = SessionManager(session_factory=_factory(created), lock=MemoryStore(), idle_seconds=10)

    await manager.handle("C1", "111.0", "one")
    assert created[0].closed is False

    await manager.sweep(now=time.time() + 1_000_000)  # far in the future -> idle

    assert created[0].closed is True

    await manager.handle("C1", "111.0", "two")
    assert len(created) == 2  # a fresh session was created for the next turn


async def test_sweep_leaves_recently_active_sessions_alone():
    created: list[FakeSession] = []
    manager = SessionManager(session_factory=_factory(created), lock=MemoryStore(), idle_seconds=60)

    await manager.handle("C1", "111.0", "one")
    await manager.sweep()

    assert created[0].closed is False
    assert len(created) == 1

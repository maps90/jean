from __future__ import annotations

import asyncio
import time
from pathlib import Path

from jean.db.memory import MemoryStore
from jean.session.manager import SessionManager
from jean.session.session import JeanSession
from jean.session.transcript import LocalTranscripts
from tests.test_session import MAX_TRANSCRIPT_BYTES, FakeChat, FakeSdkClient


class FakeSession:
    busy = False

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


async def test_sweep_does_not_delete_the_transcript_a_new_turn_is_running_on(tmp_path: Path):
    """close() tears the client down (SIGTERM of the CLI child -- hundreds of ms,
    and an `await` that yields the loop) and only THEN deletes the local .jsonl.
    If the sweeper pops the cache entry and closes it outside the per-thread lock,
    the user's next message -- the "first message after a lull", exactly when the
    sweeper fires -- misses the cache, builds a fresh session, hydrates the
    transcript from the store and resumes on it, all while the old close() is still
    parked in __aexit__. The delete then lands on the file the live turn is using:
    either the resume fails and a stub transcript is archived OVER the thread's only
    durable history, or the CLI keeps the unlinked inode and the thread silently
    stops replicating to Postgres. The sweeper must hold the thread lock."""
    FakeSdkClient.instances.clear()
    store, chat = MemoryStore(), FakeChat()
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))
    tearing_down, finish_teardown = asyncio.Event(), asyncio.Event()

    class SlowTeardownClient(FakeSdkClient):
        async def query(self, text: str) -> None:
            await super().query(text)
            # the CLI child appends this turn to its .jsonl as it answers
            line = b'{"turn":%d}\n' % (len(FakeSdkClient.instances),)
            local.write("sdk-session-abc", (local.read("sdk-session-abc") or b"") + line)

        async def __aexit__(self, *exc_info):
            # tearing the CLI child down takes real time and yields the loop
            tearing_down.set()
            await finish_teardown.wait()
            await super().__aexit__(*exc_info)

    def session_factory(channel: str, thread_ts: str) -> JeanSession:
        return JeanSession(
            channel,
            thread_ts,
            store=store,
            chat=chat,
            options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
            client_factory=lambda *, options: SlowTeardownClient(options=options),
            transcripts=store,
            local=local,
            max_transcript_bytes=MAX_TRANSCRIPT_BYTES,
        )

    manager = SessionManager(session_factory=session_factory, lock=MemoryStore(), idle_seconds=10)

    await manager.handle("C1", "111.0", "hello")  # turn 1: archived to the store
    assert await store.load("C1", "111.0", "sdk-session-abc") == b'{"turn":1}\n'

    sweeper = asyncio.create_task(manager.sweep(now=time.time() + 1_000_000))
    await tearing_down.wait()  # the sweeper is inside close(), parked in __aexit__

    # ... and the user's next message lands right here.
    turn = asyncio.create_task(manager.handle("C1", "111.0", "again"))
    await asyncio.sleep(0.05)  # give the new turn every chance to run
    finish_teardown.set()
    await asyncio.gather(sweeper, turn)

    # the live turn's transcript must still be on disk -- the sweeper's delete
    # belonged to a session nobody is using any more, not to this one.
    assert local.read("sdk-session-abc") == b'{"turn":1}\n{"turn":2}\n'
    assert await store.load("C1", "111.0", "sdk-session-abc") == b'{"turn":1}\n{"turn":2}\n'


async def test_sweep_leaves_a_session_that_took_a_turn_while_it_waited_for_the_lock(
    tmp_path: Path,
):
    """The sweeper now blocks on the thread lock, so a turn can start (and finish)
    while it waits. Idleness must be re-checked once the lock is held: closing the
    session the turn just ran on would throw away a freshly connected client."""
    created: list[FakeSession] = []
    lock = MemoryStore()
    manager = SessionManager(session_factory=_factory(created), lock=lock, idle_seconds=10)
    key = ("C1", "111.0")

    await manager.handle("C1", "111.0", "one")

    async with lock("C1", "111.0"):  # hold the thread lock; the sweeper must wait
        sweeper = asyncio.create_task(manager.sweep(now=time.time() + 1_000_000))
        await asyncio.sleep(0)  # let it block on the lock
        manager._last_touch[key] = time.time() + 1_000_000  # a turn ran meanwhile

    await sweeper

    assert created[0].closed is False
    assert manager._cache.get(key) is created[0]


async def test_sweep_never_closes_a_session_whose_turn_is_in_flight(tmp_path: Path):
    """`_last_touch` is stamped BEFORE the turn, and a turn parked on a human
    approval runs longer than idle_seconds (approval_ttl is 30 min, idle_minutes 15),
    so "idle" does not mean "not running". Sweeping such a session tears down the
    client and deletes the .jsonl the CLI child still has open: the turn then
    archives nothing and the thread silently rewinds to its last archived turn."""
    FakeSdkClient.instances.clear()
    store, chat = MemoryStore(), FakeChat()
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))
    started, release = asyncio.Event(), asyncio.Event()

    class ApprovalParkedClient(FakeSdkClient):
        async def query(self, text: str) -> None:
            await super().query(text)
            # the CLI child appends to its transcript as the turn runs ...
            line = b'{"turn":%d}\n' % len(self.queried)
            local.write("sdk-session-abc", (local.read("sdk-session-abc") or b"") + line)
            if len(self.queried) > 1:  # ... and turn 2 parks on a human approval
                started.set()
                await release.wait()

    session = JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
        client_factory=lambda *, options: ApprovalParkedClient(options=options),
        transcripts=store,
        local=local,
        max_transcript_bytes=MAX_TRANSCRIPT_BYTES,
    )
    manager = SessionManager(
        session_factory=lambda channel, thread_ts: session,
        lock=MemoryStore(),
        idle_seconds=10,
    )
    key = ("C1", "111.0")

    await manager.handle("C1", "111.0", "hello")  # turn 1 archives cleanly

    turn = asyncio.create_task(manager.handle("C1", "111.0", "deploy prod?"))
    await started.wait()
    entered_at = manager._last_touch[key]

    await manager.sweep(now=time.time() + 1_000_000)  # the idle sweeper fires mid-turn

    assert session._client is not None, "the CLI child must survive its own turn"
    assert manager._cache.get(key) is session  # left cached for a later sweep
    assert local.read("sdk-session-abc") == b'{"turn":1}\n{"turn":2}\n'  # transcript intact

    release.set()
    await turn

    # the turn finished on a live transcript and archived it
    assert await store.load("C1", "111.0", "sdk-session-abc") == b'{"turn":1}\n{"turn":2}\n'
    # a long turn must not come back already-idle
    assert manager._last_touch[key] > entered_at

    await manager.sweep(now=time.time() + 1_000_000)  # no longer busy -> now it goes

    assert session._client is None
    assert manager._cache.get(key) is None

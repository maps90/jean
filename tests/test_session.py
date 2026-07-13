from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import ProcessError

from jean.db.memory import MemoryStore
from jean.session.session import JeanSession, RoutingContext
from jean.session.transcript import LocalTranscripts


@dataclass
class FakeResultMessage:
    session_id: str


class FakeSdkClient:
    """Stands in for ClaudeSDKClient: an async context manager whose
    query()/receive_response() are driven by the test."""

    instances: list[FakeSdkClient] = []

    def __init__(self, *, options):
        self.options = options
        self.queried: list[str] = []
        self.entered = False
        self.exited = False
        FakeSdkClient.instances.append(self)

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc_info):
        self.exited = True

    async def query(self, text: str) -> None:
        self.queried.append(text)

    async def receive_response(self):
        yield FakeResultMessage(session_id="sdk-session-abc")


class FakeChat:
    def __init__(self):
        self.statuses: list[tuple[str, str, str]] = []
        self.replies: list[tuple[str, str, str]] = []

    async def set_status(self, channel, thread_ts, status):
        self.statuses.append((channel, thread_ts, status))

    async def reply(self, channel, thread_ts, text):
        self.replies.append((channel, thread_ts, text))
        return "999.0"

    async def edit(self, *a, **k):
        raise NotImplementedError

    async def upload(self, *a, **k):
        raise NotImplementedError

    async def react(self, *a, **k):
        raise NotImplementedError

    async def unreact(self, *a, **k):
        raise NotImplementedError


def _client_factory():
    calls: list[dict] = []

    def factory(*, options):
        calls.append({"options": options})
        return FakeSdkClient(options=options)

    return factory, calls


async def test_run_turn_persists_session_id_and_sets_status(tmp_path: Path):
    FakeSdkClient.instances.clear()
    store = MemoryStore()
    chat = FakeChat()
    routing = RoutingContext()
    factory, calls = _client_factory()

    session = JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        routing=routing,
        options_factory=lambda resume: {"resume": resume},
        client_factory=factory,
        transcripts=store,
        local=LocalTranscripts(cli_home=tmp_path, cwd=Path("/w")),
    )

    await session.run_turn("hello")

    row = await store.get_session("C1", "111.0")
    assert row.sdk_session_id == "sdk-session-abc"
    assert calls[0]["options"] == {"resume": None}  # first turn: nothing to resume
    assert routing.channel == "C1"
    assert routing.thread_ts == "111.0"
    assert ("C1", "111.0", "is thinking...") in chat.statuses


async def test_run_turn_reuses_the_connected_client_across_turns(tmp_path: Path):
    FakeSdkClient.instances.clear()
    store = MemoryStore()
    chat = FakeChat()
    routing = RoutingContext()
    factory, calls = _client_factory()

    session = JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        routing=routing,
        options_factory=lambda resume: {"resume": resume},
        client_factory=factory,
        transcripts=store,
        local=LocalTranscripts(cli_home=tmp_path, cwd=Path("/w")),
    )

    await session.run_turn("hello")
    await session.run_turn("again")

    assert len(calls) == 1  # same JeanSession -> client built once, reused
    assert FakeSdkClient.instances[0].queried == ["hello", "again"]


async def test_second_turn_on_a_fresh_session_resumes_stored_id(tmp_path: Path):
    """Simulates a different worker (or a rebuilt cache entry) picking up the
    same thread: resume must come from the store, not from any in-process
    state."""
    FakeSdkClient.instances.clear()
    store = MemoryStore()
    chat = FakeChat()
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))

    routing1 = RoutingContext()
    factory1, calls1 = _client_factory()
    session1 = JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        routing=routing1,
        options_factory=lambda resume: {"resume": resume},
        client_factory=factory1,
        transcripts=store,
        local=local,
    )
    await session1.run_turn("hello")
    assert calls1[0]["options"] == {"resume": None}

    routing2 = RoutingContext()
    factory2, calls2 = _client_factory()
    session2 = JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        routing=routing2,
        options_factory=lambda resume: {"resume": resume},
        client_factory=factory2,
        transcripts=store,
        local=local,
    )
    await session2.run_turn("continue")
    assert calls2[0]["options"] == {"resume": "sdk-session-abc"}


async def test_failed_turn_resets_client_to_none_and_next_turn_rebuilds(tmp_path: Path):
    """I3: a client whose first turn raises must not be left poisoned
    (non-None and un-entered) -- the next run_turn should rebuild a fresh
    client and resume from the stored sdk_session_id."""
    FakeSdkClient.instances.clear()
    store = MemoryStore()
    chat = FakeChat()
    routing = RoutingContext()

    class ExplodingClient(FakeSdkClient):
        async def query(self, text: str) -> None:
            await super().query(text)
            raise RuntimeError("boom")

    calls: list[dict] = []

    def factory(*, options):
        calls.append({"options": options})
        return ExplodingClient(options=options)

    session = JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        routing=routing,
        options_factory=lambda resume: {"resume": resume},
        client_factory=factory,
        transcripts=store,
        local=LocalTranscripts(cli_home=tmp_path, cwd=Path("/w")),
    )

    try:
        await session.run_turn("hello")
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected the exploding turn to raise")

    assert session._client is None
    assert len(calls) == 1

    # A session_id was never persisted (turn failed before receive_response),
    # so the next factory call should be given the same stored resume (None).
    def factory2(*, options):
        calls.append({"options": options})
        return FakeSdkClient(options=options)

    session._client_factory = factory2
    await session.run_turn("again")

    assert len(calls) == 2
    assert calls[1]["options"] == {"resume": None}
    assert session._client is not None


async def test_close_disconnects_the_client(tmp_path: Path):
    FakeSdkClient.instances.clear()
    store = MemoryStore()
    chat = FakeChat()
    routing = RoutingContext()
    factory, _calls = _client_factory()

    session = JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        routing=routing,
        options_factory=lambda resume: {"resume": resume},
        client_factory=factory,
        transcripts=store,
        local=LocalTranscripts(cli_home=tmp_path, cwd=Path("/w")),
    )
    await session.run_turn("hello")
    await session.close()

    assert FakeSdkClient.instances[0].exited is True


async def test_stale_resume_falls_back_to_a_fresh_session(tmp_path: Path):
    """The claude CLI keeps each conversation's transcript on the local
    filesystem while jean persists only the id in Postgres, so a restarted pod
    (or a second replica) resumes an id whose transcript it cannot see and the
    CLI exits 1 before the first turn. That must not kill the thread: jean
    reconnects without `resume` and carries on."""
    FakeSdkClient.instances.clear()
    store = MemoryStore()
    chat = FakeChat()
    routing = RoutingContext()
    await store.upsert_session("C1", "111.0", sdk_session_id="gone-with-the-pod")

    calls: list[dict] = []

    class ResumeRejectingClient(FakeSdkClient):
        async def __aenter__(self):
            if self.options["resume"] is not None:
                # what claude_agent_sdk raises when `claude --resume <id>`
                # prints "No conversation found with session ID" and exits 1
                raise ProcessError("Command failed with exit code 1", exit_code=1)
            return await super().__aenter__()

    def factory(*, options):
        calls.append({"options": options})
        return ResumeRejectingClient(options=options)

    session = JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        routing=routing,
        options_factory=lambda resume: {"resume": resume},
        client_factory=factory,
        transcripts=store,
        local=LocalTranscripts(cli_home=tmp_path, cwd=Path("/w")),
    )

    await session.run_turn("hello")

    assert [c["options"]["resume"] for c in calls] == ["gone-with-the-pod", None]
    assert FakeSdkClient.instances[-1].queried == ["hello"]
    # the fresh turn's ResultMessage replaces the unusable id in the store
    row = await store.get_session("C1", "111.0")
    assert row.sdk_session_id == "sdk-session-abc"
    # the lost context is announced, not silently swallowed
    assert len(chat.replies) == 1
    assert "fresh" in chat.replies[0][2].lower()


async def test_connect_failure_unrelated_to_resume_propagates(tmp_path: Path):
    """A startup failure that is NOT about the resume id (a bad --plugin-dir,
    bad auth) also exits 1. Reconnecting without `resume` fails identically, so
    jean must surface the error rather than pretend the thread lost its memory
    and destroy the stored session id."""
    FakeSdkClient.instances.clear()
    store = MemoryStore()
    chat = FakeChat()
    routing = RoutingContext()
    await store.upsert_session("C1", "111.0", sdk_session_id="perfectly-good-id")

    calls: list[dict] = []

    class AlwaysFailingClient(FakeSdkClient):
        async def __aenter__(self):
            raise ProcessError("Command failed with exit code 1", exit_code=1)

    def factory(*, options):
        calls.append({"options": options})
        return AlwaysFailingClient(options=options)

    session = JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        routing=routing,
        options_factory=lambda resume: {"resume": resume},
        client_factory=factory,
        transcripts=store,
        local=LocalTranscripts(cli_home=tmp_path, cwd=Path("/w")),
    )

    try:
        await session.run_turn("hello")
    except ProcessError:
        pass
    else:
        raise AssertionError("expected the failing connect to raise")

    assert session._client is None
    row = await store.get_session("C1", "111.0")
    assert row.sdk_session_id == "perfectly-good-id"  # untouched
    assert chat.replies == []  # no bogus "I lost my memory" note


def _session(store, chat, factory, local, **kw):
    return JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        routing=RoutingContext(),
        options_factory=lambda resume: {"resume": resume},
        client_factory=factory,
        transcripts=store,
        local=local,
        **kw,
    )


async def test_turn_archives_the_transcript_to_the_store(tmp_path: Path):
    FakeSdkClient.instances.clear()
    store, chat = MemoryStore(), FakeChat()
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))
    factory, _calls = _client_factory()

    session = _session(store, chat, factory, local)

    # stand in for the CLI child writing its transcript during the turn
    class WritingClient(FakeSdkClient):
        async def query(self, text: str) -> None:
            await super().query(text)
            local.write("sdk-session-abc", b'{"type":"user"}\n')

    session._client_factory = lambda *, options: WritingClient(options=options)
    await session.run_turn("hello")

    assert await store.load("C1", "111.0", "sdk-session-abc") == b'{"type":"user"}\n'
    assert (await store.get_session("C1", "111.0")).turn_seq == 1


async def test_cold_worker_hydrates_the_transcript_before_resuming(tmp_path: Path):
    """The whole point: a worker that has never seen this thread must materialize
    the transcript from Postgres onto its own disk, or `resume` finds no file."""
    FakeSdkClient.instances.clear()
    store, chat = MemoryStore(), FakeChat()
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))
    await store.upsert_session("C1", "111.0", sdk_session_id="sdk-session-abc")
    await store.save("C1", "111.0", "sdk-session-abc", b'{"type":"user"}\n')

    factory, calls = _client_factory()
    session = _session(store, chat, factory, local)

    assert local.read("sdk-session-abc") is None  # cold disk

    await session.run_turn("continue")

    assert local.read("sdk-session-abc") == b'{"type":"user"}\n'  # hydrated
    assert calls[0]["options"] == {"resume": "sdk-session-abc"}
    assert chat.replies == []  # no "I lost my memory" note -- it didn't


async def test_cached_client_is_dropped_when_another_worker_advanced_the_thread(
    tmp_path: Path,
):
    """Two JeanSessions over one store = two workers on one thread. Worker A's
    cached client skips _connect(), so without a turn_seq guard it would answer
    from a history that never saw worker B's turn and overwrite B's transcript."""
    FakeSdkClient.instances.clear()
    store, chat = MemoryStore(), FakeChat()
    local_a = LocalTranscripts(cli_home=tmp_path / "a", cwd=Path("/w"))
    local_b = LocalTranscripts(cli_home=tmp_path / "b", cwd=Path("/w"))

    factory_a, calls_a = _client_factory()
    factory_b, _calls_b = _client_factory()
    worker_a = _session(store, chat, factory_a, local_a)
    worker_b = _session(store, chat, factory_b, local_b)

    await worker_a.run_turn("first")
    assert len(calls_a) == 1

    await worker_b.run_turn("second")  # worker B advances the thread

    await worker_a.run_turn("third")  # back to A, whose cached client is now stale

    assert len(calls_a) == 2, "worker A must rebuild its client, not reuse the stale one"
    assert calls_a[1]["options"] == {"resume": "sdk-session-abc"}


async def test_local_transcript_is_kept_when_archiving_fails(tmp_path: Path):
    """A DB outage must not destroy the only copy: close() deletes the pod's local
    file only when the store definitely has it."""
    FakeSdkClient.instances.clear()
    store, chat = MemoryStore(), FakeChat()
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))

    class BrokenTranscripts:
        async def save(self, *a, **k):
            raise RuntimeError("db down")

        async def load(self, *a, **k):
            return None

    factory, _calls = _client_factory()
    session = JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        routing=RoutingContext(),
        options_factory=lambda resume: {"resume": resume},
        client_factory=factory,
        transcripts=BrokenTranscripts(),
        local=local,
    )

    class WritingClient(FakeSdkClient):
        async def query(self, text: str) -> None:
            await super().query(text)
            local.write("sdk-session-abc", b'{"type":"user"}\n')

    session._client_factory = lambda *, options: WritingClient(options=options)

    await session.run_turn("hello")  # the turn still succeeds; the user got an answer
    await session.close()

    assert local.read("sdk-session-abc") == b'{"type":"user"}\n'  # not deleted


class FlakyTranscripts:
    """A TranscriptStore whose save() can be broken and repaired mid-test, so a
    transient DB failure can be replayed. Delegates to a real MemoryStore, and
    records the order of the writes run_turn makes after a turn."""

    def __init__(self, inner: MemoryStore) -> None:
        self._inner = inner
        self.calls: list[str] = []
        self.fail_save = False

    def __getattr__(self, name):  # get_session / upsert_session / ... pass through
        return getattr(self._inner, name)

    async def bump_turn(self, channel: str, thread_ts: str) -> int:
        self.calls.append("bump_turn")
        return await self._inner.bump_turn(channel, thread_ts)

    async def save(self, channel: str, thread_ts: str, sdk_session_id: str, data: bytes) -> None:
        self.calls.append("save")
        if self.fail_save:
            raise RuntimeError("db down")
        await self._inner.save(channel, thread_ts, sdk_session_id, data)

    async def load(self, channel: str, thread_ts: str, sdk_session_id: str) -> bytes | None:
        return await self._inner.load(channel, thread_ts, sdk_session_id)


def _writing_client_factory(local: LocalTranscripts, content: dict[str, bytes | None]):
    """A client whose turn writes whatever the test currently has in `content`
    -- i.e. stands in for the CLI child appending to its transcript file."""

    calls: list[dict] = []

    class WritingClient(FakeSdkClient):
        async def query(self, text: str) -> None:
            await super().query(text)
            data = content["data"]
            if data is not None:
                local.write("sdk-session-abc", data)

    def factory(*, options):
        calls.append({"options": options})
        return WritingClient(options=options)

    return factory, calls


async def test_oversize_transcript_is_not_archived_and_survives_close(tmp_path: Path):
    """An early return from _archive (here: over max_transcript_bytes) must clear
    the archived flag left by an EARLIER successful archive -- otherwise close()
    deletes a local file the store never took and the thread silently rewinds to
    the last archived turn."""
    FakeSdkClient.instances.clear()
    store, chat = MemoryStore(), FakeChat()
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))
    content: dict[str, bytes | None] = {"data": b'{"turn":1}\n'}
    factory, _calls = _writing_client_factory(local, content)

    session = _session(store, chat, factory, local, max_transcript_bytes=32)

    await session.run_turn("hello")  # small: archived
    assert await store.load("C1", "111.0", "sdk-session-abc") == b'{"turn":1}\n'

    content["data"] = b'{"turn":1}\n' + b'{"turn":2 padded to over the cap}\n'
    await session.run_turn("again")  # now over the cap: NOT archived

    # the store still holds only turn 1 ...
    assert await store.load("C1", "111.0", "sdk-session-abc") == b'{"turn":1}\n'
    # ... so the local file is the only copy of turn 2 and close() must keep it
    await session.close()
    assert local.read("sdk-session-abc") == content["data"]


async def test_hydration_does_not_clobber_a_newer_unarchived_local_transcript(tmp_path: Path):
    """close() preserves a local transcript the store failed to take -- and the
    next cold connect on this same worker must not then overwrite it with the
    store's OLDER blob, which would undo exactly that protection."""
    FakeSdkClient.instances.clear()
    inner = MemoryStore()
    store = FlakyTranscripts(inner)
    chat = FakeChat()
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))
    content: dict[str, bytes | None] = {"data": b'{"turn":1}\n'}
    factory, calls = _writing_client_factory(local, content)

    session = _session(store, chat, factory, local)

    await session.run_turn("hello")  # turn 1 archives cleanly
    assert await store.load("C1", "111.0", "sdk-session-abc") == b'{"turn":1}\n'

    store.fail_save = True
    content["data"] = b'{"turn":1}\n{"turn":2}\n'
    await session.run_turn("again")  # turn 2: save() fails transiently
    assert await store.load("C1", "111.0", "sdk-session-abc") == b'{"turn":1}\n'  # store is behind

    await session.close()  # keeps the un-archived local file
    assert local.read("sdk-session-abc") == b'{"turn":1}\n{"turn":2}\n'

    store.fail_save = False
    content["data"] = None  # turn 3's client appends nothing; only _connect writes
    await session.run_turn("more")

    # the store's stale turn-1 blob must NOT have been written over our turn-2 file
    assert local.read("sdk-session-abc") == b'{"turn":1}\n{"turn":2}\n'
    assert calls[-1]["options"] == {"resume": "sdk-session-abc"}
    assert chat.replies == []  # the resume worked; no "I lost my memory" note
    # ... and this turn heals the store
    assert await store.load("C1", "111.0", "sdk-session-abc") == b'{"turn":1}\n{"turn":2}\n'


async def test_turn_seq_is_bumped_before_the_transcript_is_archived(tmp_path: Path):
    """If save() lands and bump_turn() then fails, the store holds a NEW transcript
    under an OLD turn_seq: other workers' cached clients still look current, keep a
    stale history and archive it over the good one. Bumping first inverts that --
    a failed save leaves an OLD transcript under a NEW turn_seq, so every other
    worker drops its cached client and re-hydrates a coherent (if older) history."""
    FakeSdkClient.instances.clear()
    inner = MemoryStore()
    store = FlakyTranscripts(inner)
    chat = FakeChat()
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))
    content: dict[str, bytes | None] = {"data": b'{"turn":1}\n'}
    factory, _calls = _writing_client_factory(local, content)

    session = _session(store, chat, factory, local)

    await session.run_turn("hello")
    assert store.calls == ["bump_turn", "save"]

    store.calls.clear()
    store.fail_save = True
    content["data"] = b'{"turn":1}\n{"turn":2}\n'
    await session.run_turn("again")  # save() fails; the turn still succeeds

    assert store.calls == ["bump_turn", "save"]
    # turn_seq advanced even though the archive did not: another worker holding a
    # cached client for seq 1 now sees 2, drops it, and re-hydrates.
    assert (await store.get_session("C1", "111.0")).turn_seq == 2

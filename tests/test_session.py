from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage as RealAssistantMessage
from claude_agent_sdk import ProcessError

from jean.db.memory import MemoryStore
from jean.session.session import ASSISTANT_MESSAGE_CLASS_NAME, JeanSession
from jean.session.transcript import LocalTranscripts

# JeanSession takes its cap from Settings.transcript_max_mb (server.py wires it in),
# so there is no default to fall back on -- tests that don't care about the cap pass
# the production value.
MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024

# The settle wait before archiving is real time, so tests that reach it drive it fast
# (its production defaults are seconds). Tests whose fake client never writes a
# transcript never reach it at all -- there is no file to settle -- and the ones whose
# fake streams no AssistantMessage owe the .jsonl no `assistant` record, so they settle
# as soon as the file has been quiet for `settle_quiet`.
FAST_SETTLE = {"settle_timeout": 0.05, "settle_interval": 0.002, "settle_quiet": 0.004}


@dataclass
class FakeResultMessage:
    session_id: str


class AssistantMessage:
    """Stands in for claude_agent_sdk.AssistantMessage -- deliberately under the SDK's
    own class name, because the NAME is what JeanSession matches on. The SDK streams
    exactly one of these per `assistant` record the CLI writes to the .jsonl, and
    run_turn counts them STRUCTURALLY (by class name) to know how many records this
    turn still owes the transcript: session/ is domain code and may not import the SDK
    (CLAUDE.md's layering rule)."""


def test_assistant_message_class_name_matches_the_real_sdk_class():
    """session/session.py cannot `import claude_agent_sdk` (CLAUDE.md's layering
    rule forbids domain code touching the SDK), so run_turn cannot `isinstance()`
    against the real `AssistantMessage` class. Instead it matches on the class's
    NAME, via the `ASSISTANT_MESSAGE_CLASS_NAME` constant.

    That is a soft dependency on a string staying in sync with the SDK. If
    claude_agent_sdk ever renames AssistantMessage, the structural match in run_turn
    silently stops counting: `streamed` collapses to 0 every turn, the settle target
    collapses to `baseline + 0`, and every turn's transcript is archived before the
    CLI finishes writing it -- the original data-loss bug this module exists to
    prevent, resurrected, with a fully green test suite (see _settle's
    `count_reliable` fallback for the runtime backstop for that same failure).

    This test is what turns that silent drift into a loud, immediate CI failure: it
    imports the real SDK class and asserts its `__name__` is exactly the string the
    domain matches on. If either side renames, this test breaks.
    """
    assert RealAssistantMessage.__name__ == ASSISTANT_MESSAGE_CLASS_NAME


class FakeSdkClient:
    """Stands in for ClaudeSDKClient: an async context manager whose
    query()/receive_response() are driven by the test."""

    instances: list[FakeSdkClient] = []

    def __init__(self, *, options):
        self.options = options
        self.queried: list[str] = []
        self.modes: list[str] = []  # set_permission_mode calls, in order
        self.entered = False
        self.exited = False
        FakeSdkClient.instances.append(self)

    async def set_permission_mode(self, mode: str) -> None:
        self.modes.append(mode)

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
    factory, calls = _client_factory()

    session = JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
        client_factory=factory,
        transcripts=store,
        local=LocalTranscripts(cli_home=tmp_path, cwd=Path("/w")),
        max_transcript_bytes=MAX_TRANSCRIPT_BYTES,
    )

    await session.run_turn("hello")

    row = await store.get_session("C1", "111.0")
    assert row.sdk_session_id == "sdk-session-abc"
    assert calls[0]["options"]["resume"] is None  # first turn: nothing to resume
    assert ("C1", "111.0", "is thinking...") in chat.statuses


async def test_run_turn_reuses_the_connected_client_across_turns(tmp_path: Path):
    FakeSdkClient.instances.clear()
    store = MemoryStore()
    chat = FakeChat()
    factory, calls = _client_factory()

    session = JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
        client_factory=factory,
        transcripts=store,
        local=LocalTranscripts(cli_home=tmp_path, cwd=Path("/w")),
        max_transcript_bytes=MAX_TRANSCRIPT_BYTES,
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

    factory1, calls1 = _client_factory()
    session1 = JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
        client_factory=factory1,
        transcripts=store,
        local=local,
        max_transcript_bytes=MAX_TRANSCRIPT_BYTES,
    )
    await session1.run_turn("hello")
    assert calls1[0]["options"]["resume"] is None

    factory2, calls2 = _client_factory()
    session2 = JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
        client_factory=factory2,
        transcripts=store,
        local=local,
        max_transcript_bytes=MAX_TRANSCRIPT_BYTES,
    )
    await session2.run_turn("continue")
    assert calls2[0]["options"]["resume"] == "sdk-session-abc"


async def test_failed_turn_resets_client_to_none_and_next_turn_rebuilds(tmp_path: Path):
    """I3: a client whose first turn raises must not be left poisoned
    (non-None and un-entered) -- the next run_turn should rebuild a fresh
    client and resume from the stored sdk_session_id."""
    FakeSdkClient.instances.clear()
    store = MemoryStore()
    chat = FakeChat()

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
        options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
        client_factory=factory,
        transcripts=store,
        local=LocalTranscripts(cli_home=tmp_path, cwd=Path("/w")),
        max_transcript_bytes=MAX_TRANSCRIPT_BYTES,
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
    assert calls[1]["options"]["resume"] is None
    assert session._client is not None


async def test_close_disconnects_the_client(tmp_path: Path):
    FakeSdkClient.instances.clear()
    store = MemoryStore()
    chat = FakeChat()
    factory, _calls = _client_factory()

    session = JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
        client_factory=factory,
        transcripts=store,
        local=LocalTranscripts(cli_home=tmp_path, cwd=Path("/w")),
        max_transcript_bytes=MAX_TRANSCRIPT_BYTES,
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
        options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
        client_factory=factory,
        transcripts=store,
        local=LocalTranscripts(cli_home=tmp_path, cwd=Path("/w")),
        max_transcript_bytes=MAX_TRANSCRIPT_BYTES,
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
        options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
        client_factory=factory,
        transcripts=store,
        local=LocalTranscripts(cli_home=tmp_path, cwd=Path("/w")),
        max_transcript_bytes=MAX_TRANSCRIPT_BYTES,
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
    kw.setdefault("max_transcript_bytes", MAX_TRANSCRIPT_BYTES)
    for k, v in FAST_SETTLE.items():
        kw.setdefault(k, v)
    return JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
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
    assert calls[0]["options"]["resume"] == "sdk-session-abc"
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
    assert calls_a[1]["options"]["resume"] == "sdk-session-abc"


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
        options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
        client_factory=factory,
        transcripts=BrokenTranscripts(),
        local=local,
        max_transcript_bytes=MAX_TRANSCRIPT_BYTES,
        **FAST_SETTLE,
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
        self.fail_bump = False

    def __getattr__(self, name):  # get_session / upsert_session / ... pass through
        return getattr(self._inner, name)

    async def bump_turn(self, channel: str, thread_ts: str) -> int:
        self.calls.append("bump_turn")
        if self.fail_bump:
            raise RuntimeError("connection reset")
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


def _appending_client_factory(local: LocalTranscripts, line: dict[str, bytes]):
    """A client whose turn APPENDS the test's current `line` to whatever transcript
    is on THIS worker's disk -- i.e. the CLI child continuing the conversation it
    resumed, so the file it leaves behind shows which history the turn ran on."""

    calls: list[dict] = []

    class AppendingClient(FakeSdkClient):
        async def query(self, text: str) -> None:
            await super().query(text)
            local.write("sdk-session-abc", (local.read("sdk-session-abc") or b"") + line["data"])

    def factory(*, options):
        calls.append({"options": options})
        return AppendingClient(options=options)

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
    assert calls[-1]["options"]["resume"] == "sdk-session-abc"
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


async def test_unarchived_local_transcript_loses_to_a_turn_another_worker_committed(
    tmp_path: Path,
):
    """Two JeanSessions over one store = two workers on one thread. Worker A's
    save() fails, so A keeps an un-archived local file -- but then worker B commits
    a turn on top of the store's (older) copy, advancing turn_seq past A. A's file
    is now not *newer* than the store, merely a divergent branch. On A's next turn
    it must hydrate the store anyway: preferring its own file would archive a
    history missing B's turn over B's, destroying an answer the user already saw."""
    FakeSdkClient.instances.clear()
    inner = MemoryStore()
    store_a = FlakyTranscripts(inner)  # worker A's view of the store: save() can fail
    chat = FakeChat()
    local_a = LocalTranscripts(cli_home=tmp_path / "a", cwd=Path("/w"))
    local_b = LocalTranscripts(cli_home=tmp_path / "b", cwd=Path("/w"))

    line_a: dict[str, bytes] = {"data": b'{"turn":1,"by":"a"}\n'}
    line_b: dict[str, bytes] = {"data": b'{"turn":2,"by":"b"}\n'}
    factory_a, calls_a = _appending_client_factory(local_a, line_a)
    factory_b, _calls_b = _appending_client_factory(local_b, line_b)

    worker_a = _session(store_a, chat, factory_a, local_a)
    worker_b = _session(inner, chat, factory_b, local_b)

    store_a.fail_save = True
    await worker_a.run_turn("first")  # turn_seq -> 1, but the archive fails
    store_a.fail_save = False
    assert await inner.load("C1", "111.0", "sdk-session-abc") is None  # store has nothing
    assert local_a.read("sdk-session-abc") == b'{"turn":1,"by":"a"}\n'  # only A has turn 1

    await worker_b.run_turn("second")  # B commits turn 2 on the store's history
    assert await inner.load("C1", "111.0", "sdk-session-abc") == b'{"turn":2,"by":"b"}\n'
    assert (await inner.get_session("C1", "111.0")).turn_seq == 2  # the store moved past A

    line_a["data"] = b'{"turn":3,"by":"a"}\n'
    await worker_a.run_turn("third")  # A's cached client is stale -> close() + _connect

    # A resumed the STORE's transcript, not its own stale local branch ...
    assert calls_a[1]["options"]["resume"] == "sdk-session-abc"
    assert local_a.read("sdk-session-abc") == b'{"turn":2,"by":"b"}\n{"turn":3,"by":"a"}\n'
    # ... so B's turn is still there, not overwritten by A's un-archived turn 1
    assert (
        await inner.load("C1", "111.0", "sdk-session-abc")
        == b'{"turn":2,"by":"b"}\n{"turn":3,"by":"a"}\n'
    )
    assert chat.replies == []  # the resume worked; no "I lost my memory" note


async def test_turn_that_fails_after_the_cli_wrote_keeps_the_local_transcript(tmp_path: Path):
    """The store's copy stops being up to date the moment the CLI child writes, not
    when _archive runs -- so `_archived` must be cleared before query(). Otherwise a
    turn that fails in between (here: bump_turn on a dropped connection) leaves a
    `True` left over from the PREVIOUS turn, and close() deletes the only copy of
    the turn that just ran."""
    FakeSdkClient.instances.clear()
    inner = MemoryStore()
    store = FlakyTranscripts(inner)
    chat = FakeChat()
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))
    content: dict[str, bytes | None] = {"data": b'{"turn":1}\n'}
    factory, _calls = _writing_client_factory(local, content)

    session = _session(store, chat, factory, local)

    await session.run_turn("hello")  # turn 1 archives cleanly -> _archived True
    assert await store.load("C1", "111.0", "sdk-session-abc") == b'{"turn":1}\n'

    # turn 2: the CLI writes its line and the user gets their answer, then the
    # store write fails -- the turn raises before _archive is ever reached.
    store.fail_bump = True
    content["data"] = b'{"turn":1}\n{"turn":2}\n'
    with pytest.raises(RuntimeError):
        await session.run_turn("again")

    assert await store.load("C1", "111.0", "sdk-session-abc") == b'{"turn":1}\n'  # store is behind

    await session.close()

    # the local file is the ONLY copy of turn 2 -- close() must not delete it
    assert local.read("sdk-session-abc") == b'{"turn":1}\n{"turn":2}\n'


async def test_stale_local_transcript_is_deleted_when_the_resume_is_refused(tmp_path: Path):
    """When hydration is SKIPPED (our local file was the newer un-archived copy) and
    the resume is then refused, the turn gets a NEW session id -- nothing will ever
    reference the old one again, so its file must not be orphaned on disk."""
    FakeSdkClient.instances.clear()
    inner = MemoryStore()
    store = FlakyTranscripts(inner)
    chat = FakeChat()
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))
    content: dict[str, bytes | None] = {"data": b'{"turn":1}\n'}
    factory, _calls = _writing_client_factory(local, content)

    session = _session(store, chat, factory, local)

    store.fail_save = True
    await session.run_turn("hello")  # archive fails -> our local file is un-archived
    assert await inner.load("C1", "111.0", "sdk-session-abc") is None
    assert local.read("sdk-session-abc") == b'{"turn":1}\n'
    await session.close()  # keeps the un-archived file, drops the client

    class ResumeRejectingClient(FakeSdkClient):
        async def __aenter__(self):
            if self.options["resume"] is not None:
                raise ProcessError("Command failed with exit code 1", exit_code=1)
            return await super().__aenter__()

        async def receive_response(self):
            yield FakeResultMessage(session_id="sdk-session-new")

    session._client_factory = lambda *, options: ResumeRejectingClient(options=options)
    store.fail_save = False

    await session.run_turn("again")  # the CLI refuses the resume -> fresh session

    assert (await inner.get_session("C1", "111.0")).sdk_session_id == "sdk-session-new"
    # the refused id's transcript is dead weight: no id references it any more
    assert local.path("sdk-session-abc").exists() is False


async def test_close_survives_a_client_whose_teardown_raises(tmp_path: Path):
    """run_turn's staleness branch calls close() mid-turn, so close() is on the hot
    path: a client we are DISCARDING whose __aexit__ raises must not fail the user's
    turn, nor be left behind in _client."""
    FakeSdkClient.instances.clear()
    store, chat = MemoryStore(), FakeChat()
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))

    class ExitFailingClient(FakeSdkClient):
        async def __aexit__(self, *exc_info):
            await super().__aexit__(*exc_info)
            raise RuntimeError("teardown blew up")

    calls: list[dict] = []

    def factory(*, options):
        calls.append({"options": options})
        return ExitFailingClient(options=options)

    session = _session(store, chat, factory, local)
    await session.run_turn("hello")

    # another worker advances the thread: our cached client is stale, so the next
    # turn tears it down mid-turn -- and its teardown blows up.
    await store.bump_turn("C1", "111.0")

    await session.run_turn("again")  # must not raise

    assert len(calls) == 2, "the stale client must be replaced, not reused"
    assert calls[1]["options"]["resume"] == "sdk-session-abc"
    assert FakeSdkClient.instances[-1].queried == ["again"]

    await session.close()
    assert session._client is None  # dropped even though __aexit__ raised


def _tool_turn_client_factory(local: LocalTranscripts, delay: float, answer: bytes):
    """A client that writes its transcript the way the real CLI does on a real jean
    turn -- and a real jean turn is a TOOL turn: persona/identity.py mandates that
    every visible reply goes out through `mcp__jean_slack__reply`, so the CLI emits
    SEVERAL `assistant` records per turn (a tool_use, then the final text), not one.

    The shapes that matter, measured against the real CLI:
      - the `user` record and the `assistant`/tool_use record are flushed EARLY --
        already on disk BEFORE receive_response() returns;
      - the final `assistant`/text record is flushed LATE, ~0.5s afterwards.

    So "the assistant count has risen above the baseline" is ALREADY TRUE when the
    response ends, satisfied by the tool_use record, while the answer is still
    unwritten: settling on that archives a truncated history whose last turn ends on
    an unresolved tool_use. The SDK streams one AssistantMessage per on-disk
    `assistant` record, so the exact target is `baseline + streamed`."""

    tasks: list[asyncio.Task] = []

    def _append(chunk: bytes) -> None:
        local.write("sdk-session-abc", (local.read("sdk-session-abc") or b"") + chunk)

    class ToolTurnClient(FakeSdkClient):
        async def query(self, text: str) -> None:
            await super().query(text)
            # flushed while the turn is still streaming
            _append(b'{"type":"user"}\n')
            _append(b'{"type":"assistant","tool_use":"mcp__jean_slack__reply"}\n')
            _append(b'{"type":"user","tool_result":"ok"}\n')

            async def _flush_later() -> None:
                await asyncio.sleep(delay)
                _append(b'{"type":"assistant","text":"' + answer + b'"}\n')
                await asyncio.sleep(delay / 2)
                _append(b'{"type":"system","subtype":"post-turn"}\n')  # trails the answer

            tasks.append(asyncio.create_task(_flush_later()))

        async def receive_response(self):
            # one AssistantMessage per `assistant` record the CLI will write
            yield AssistantMessage()  # the tool_use
            yield AssistantMessage()  # the final answer
            yield FakeResultMessage(session_id="sdk-session-abc")

    def factory(*, options):
        return ToolTurnClient(options=options)

    return factory, tasks


async def test_archive_waits_for_the_cli_to_flush_this_turn_s_final_answer(tmp_path: Path):
    """The CLI's transcript writes are write-behind, and a real turn writes several
    `assistant` records: the tool_use lands before receive_response() returns, the
    final text lands ~0.5s after. Archiving as soon as the count exceeds the baseline
    is therefore satisfied by the TOOL CALL and stores a history with no answer in it
    -- a cold worker hydrates that, the CLI injects "Continue from where you left
    off.", and the answer is gone from the durable copy forever."""
    FakeSdkClient.instances.clear()
    store, chat = MemoryStore(), FakeChat()
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))
    factory, tasks = _tool_turn_client_factory(local, delay=0.05, answer=b"BANANA")

    # a real settle budget: the point is that the wait OUTLASTS the CLI's write-behind
    session = _session(
        store, chat, factory, local, settle_timeout=5.0, settle_interval=0.01, settle_quiet=0.05
    )

    await session.run_turn("say BANANA")

    stored = await store.load("C1", "111.0", "sdk-session-abc")
    assert stored is not None
    assert b"BANANA" in stored, "archived before the CLI wrote this turn's final answer"
    # both assistant records -- not just the tool_use that lands early
    assert stored.count(b'"type":"assistant"') == 2
    await asyncio.gather(*tasks)  # already done -- the settle wait outlives them


async def test_second_turn_waits_for_its_own_answer_not_the_previous_one(tmp_path: Path):
    """The settle target is BASELINE + the assistant messages streamed this turn, and
    the baseline is taken before query(): turn 2 must wait for turn 2's own records,
    not be satisfied by turn 1's, which are already on disk when it starts."""
    FakeSdkClient.instances.clear()
    store, chat = MemoryStore(), FakeChat()
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))
    factory, tasks = _tool_turn_client_factory(local, delay=0.05, answer=b"BANANA")

    session = _session(
        store, chat, factory, local, settle_timeout=5.0, settle_interval=0.01, settle_quiet=0.05
    )

    await session.run_turn("say BANANA")
    await session.run_turn("say BANANA again")

    stored = await store.load("C1", "111.0", "sdk-session-abc")
    assert stored is not None
    assert stored.count(b'"type":"assistant"') == 4, "turn 2's final answer is missing"
    assert stored.count(b"BANANA") == 2
    await asyncio.gather(*tasks)


def _never_flushing_client_factory(local: LocalTranscripts):
    """A client whose turn streams an AssistantMessage the CLI never gets around to
    writing: the transcript keeps its `user` record and nothing else, forever."""

    class NeverFlushingClient(FakeSdkClient):
        async def query(self, text: str) -> None:
            await super().query(text)
            local.write("sdk-session-abc", b'{"type":"user"}\n')

        async def receive_response(self):
            yield AssistantMessage()
            yield FakeResultMessage(session_id="sdk-session-abc")

    return lambda *, options: NeverFlushingClient(options=options)


async def test_archive_happens_anyway_and_warns_when_the_transcript_never_settles(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    """If the answer never lands on disk, archive what IS there -- an incomplete
    transcript still beats none, and the user already has their answer -- but say so
    loudly. Never fail the turn over it, never swallow it."""
    FakeSdkClient.instances.clear()
    store, chat = MemoryStore(), FakeChat()
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))
    factory = _never_flushing_client_factory(local)

    session = _session(store, chat, factory, local)

    with caplog.at_level(logging.WARNING, logger="jean.session.session"):
        await session.run_turn("hello")  # must not raise

    assert await store.load("C1", "111.0", "sdk-session-abc") == b'{"type":"user"}\n'
    assert any(
        "transcript" in r.message and "last turn" in r.message
        for r in caplog.records
        if r.levelno >= logging.WARNING
    ), f"expected a loud warning, got {[r.message for r in caplog.records]}"


def _renamed_assistant_message_client_factory(local: LocalTranscripts, delay: float):
    """Simulates claude_agent_sdk renaming AssistantMessage out from under run_turn's
    structural match: this client writes a transcript exactly like a real tool turn
    (see `_tool_turn_client_factory`) -- a tool_use record flushed early, the final
    answer flushed `delay` seconds later -- but streams instances of a class that is
    NOT named "AssistantMessage". `type(msg).__name__ == ASSISTANT_MESSAGE_CLASS_NAME`
    then matches nothing, so `assistant_msgs` stays 0 for a turn that otherwise
    succeeds -- exactly the condition `_settle`'s `count_reliable` fallback exists
    to catch."""

    class RenamedAssistantMessage:
        """Stands in for what the SDK class would look like after a rename: same
        role, different `__name__`, so the structural match in run_turn cannot see
        it."""

    def _append(chunk: bytes) -> None:
        local.write("sdk-session-abc", (local.read("sdk-session-abc") or b"") + chunk)

    class RenamedClient(FakeSdkClient):
        async def query(self, text: str) -> None:
            await super().query(text)
            _append(b'{"type":"user"}\n')
            _append(b'{"type":"assistant","tool_use":"mcp__jean_slack__reply"}\n')
            _append(b'{"type":"user","tool_result":"ok"}\n')

            async def _flush_later() -> None:
                await asyncio.sleep(delay)
                _append(b'{"type":"assistant","text":"BANANA"}\n')

            asyncio.create_task(_flush_later())

        async def receive_response(self):
            # Named unlike "AssistantMessage" -- the rename this test simulates.
            yield RenamedAssistantMessage()
            yield RenamedAssistantMessage()
            yield FakeResultMessage(session_id="sdk-session-abc")

    return lambda *, options: RenamedClient(options=options)


async def test_settle_falls_back_to_quiet_wait_and_warns_when_the_sdk_class_is_unrecognized(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    """If claude_agent_sdk ever renames AssistantMessage, run_turn's structural match
    finds nothing and `assistant_msgs` stays 0 -- collapsing the naive target to
    `baseline + 0`, which the baseline alone already satisfies. Without the
    `count_reliable` fallback, `_settle` would return the instant it takes its first
    reading, archiving a transcript that ends on an unresolved tool_use -- before the
    CLI has even written this turn's answer. This pins that the fallback instead
    waits for the file to go quiet (i.e. does NOT return instantly) and warns loudly,
    so a rename becomes a slow-but-correct archive plus a loud log line, never a
    silent truncation."""
    FakeSdkClient.instances.clear()
    store, chat = MemoryStore(), FakeChat()
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))
    factory = _renamed_assistant_message_client_factory(local, delay=0.05)

    session = _session(
        store, chat, factory, local, settle_timeout=5.0, settle_interval=0.01, settle_quiet=0.05
    )

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="jean.session.session"):
        await session.run_turn("say BANANA")
    elapsed = time.monotonic() - started

    assert elapsed >= 0.05, "settle returned before the CLI's delayed write -- it did not wait"
    stored = await store.load("C1", "111.0", "sdk-session-abc")
    assert stored is not None
    assert b"BANANA" in stored, "archived before the delayed answer was written"
    assert any(
        "could not count" in r.message and "AssistantMessage" in r.message
        for r in caplog.records
        if r.levelno >= logging.WARNING
    ), (
        f"expected a loud warning about the unrecognized SDK class, got {[r.message for r in caplog.records]}"
    )


async def test_settle_wait_does_not_fire_when_there_is_no_local_transcript(tmp_path: Path):
    """Nothing on disk = nothing to settle (a misconfigured cli_home, say). Waiting
    out the full timeout on every turn of a thread that will never have a file would
    add seconds to each of them; _archive's own warning covers that case."""
    FakeSdkClient.instances.clear()
    store, chat = MemoryStore(), FakeChat()
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))
    factory, _calls = _client_factory()  # writes no transcript at all

    session = _session(store, chat, factory, local, settle_timeout=30.0, settle_interval=1.0)

    started = time.monotonic()
    await session.run_turn("hello")
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, "run_turn sat in the settle wait with no file to wait for"
    assert local.read("sdk-session-abc") is None


async def test_the_threads_permission_mode_reaches_the_sdk(tmp_path: Path):
    """/mode writes permission_mode to the store; without this the SDK was only
    ever given the deployment-wide default and the command was a no-op."""
    FakeSdkClient.instances.clear()
    store, chat = MemoryStore(), FakeChat()
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))
    factory, calls = _client_factory()
    await store.upsert_session("C1", "111.0", permission_mode="plan")

    session = _session(store, chat, factory, local)
    await session.run_turn("hello")

    assert calls[0]["options"]["mode"] == "plan"


async def test_changing_mode_mid_thread_rebuilds_the_client(tmp_path: Path):
    """permission_mode is fixed when the SDK client connects, so a cached client
    would keep the old mode forever -- `/mode bypassPermissions` (the escape
    hatch from approval prompts) would appear to do nothing until the idle sweep
    dropped the session, up to idle_minutes later.

    Note the turn_seq guard cannot stand in for this: this worker took both turns
    itself, so its cached client is perfectly current with the store (`turn_seq ==
    _seen_seq`) -- only the mode moved."""
    FakeSdkClient.instances.clear()
    store, chat = MemoryStore(), FakeChat()
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))
    factory, calls = _client_factory()

    session = _session(store, chat, factory, local)
    await session.run_turn("hello")
    assert calls[0]["options"]["mode"] is None

    await store.upsert_session("C1", "111.0", permission_mode="bypassPermissions", touch=False)
    await session.run_turn("now hurry up")

    assert len(calls) == 2  # rebuilt rather than reusing the cached client
    assert calls[1]["options"]["mode"] == "bypassPermissions"
    assert calls[1]["options"]["resume"] == "sdk-session-abc"  # same conversation
    assert FakeSdkClient.instances[0].exited is True  # old client closed, not leaked


async def test_reused_client_is_not_re_armed_to_plan(tmp_path: Path):
    """The classifier gates risky tool calls now (agent_options.classify_risk), so
    the SDK's own permission mode never flips mid-turn the way plan-approval used
    to -- there is nothing for a reused client to be put back into. A cached
    client on a plan-mode thread must be left exactly as connected across turns."""
    FakeSdkClient.instances.clear()
    store, chat = MemoryStore(), FakeChat()
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))
    factory, calls = _client_factory()
    await store.upsert_session("C1", "111.0", permission_mode="plan")

    session = _session(store, chat, factory, local)
    await session.run_turn("first request")
    client = FakeSdkClient.instances[0]
    assert client.modes == []  # fresh connect: opened in plan

    await session.run_turn("second request")

    assert len(calls) == 1  # same cached client reused (mode unchanged, seq current)
    assert client.modes == []  # never re-armed
    assert client.queried == ["first request", "second request"]


async def test_a_cached_non_plan_client_is_not_rearmed(tmp_path: Path):
    """A thread on `/mode default` (the classifier-gated mode) or
    `bypassPermissions` must be left exactly as connected across turns."""
    FakeSdkClient.instances.clear()
    store, chat = MemoryStore(), FakeChat()
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))
    factory, _calls = _client_factory()
    await store.upsert_session("C1", "111.0", permission_mode="default")

    session = _session(store, chat, factory, local)
    await session.run_turn("first")
    await session.run_turn("second")

    assert FakeSdkClient.instances[0].modes == []

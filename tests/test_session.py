from __future__ import annotations

from dataclasses import dataclass

from claude_agent_sdk import ProcessError

from jean.db.memory import MemoryStore
from jean.session.session import JeanSession, RoutingContext


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


async def test_run_turn_persists_session_id_and_sets_status():
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
        options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
        client_factory=factory,
    )

    await session.run_turn("hello")

    row = await store.get_session("C1", "111.0")
    assert row.sdk_session_id == "sdk-session-abc"
    assert calls[0]["options"]["resume"] is None  # first turn: nothing to resume
    assert routing.channel == "C1"
    assert routing.thread_ts == "111.0"
    assert ("C1", "111.0", "is thinking...") in chat.statuses


async def test_run_turn_reuses_the_connected_client_across_turns():
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
        options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
        client_factory=factory,
    )

    await session.run_turn("hello")
    await session.run_turn("again")

    assert len(calls) == 1  # same JeanSession -> client built once, reused
    assert FakeSdkClient.instances[0].queried == ["hello", "again"]


async def test_second_turn_on_a_fresh_session_resumes_stored_id():
    """Simulates a different worker (or a rebuilt cache entry) picking up the
    same thread: resume must come from the store, not from any in-process
    state."""
    FakeSdkClient.instances.clear()
    store = MemoryStore()
    chat = FakeChat()

    routing1 = RoutingContext()
    factory1, calls1 = _client_factory()
    session1 = JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        routing=routing1,
        options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
        client_factory=factory1,
    )
    await session1.run_turn("hello")
    assert calls1[0]["options"]["resume"] is None

    routing2 = RoutingContext()
    factory2, calls2 = _client_factory()
    session2 = JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        routing=routing2,
        options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
        client_factory=factory2,
    )
    await session2.run_turn("continue")
    assert calls2[0]["options"]["resume"] == "sdk-session-abc"


async def test_failed_turn_resets_client_to_none_and_next_turn_rebuilds():
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
        options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
        client_factory=factory,
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


async def test_close_disconnects_the_client():
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
        options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
        client_factory=factory,
    )
    await session.run_turn("hello")
    await session.close()

    assert FakeSdkClient.instances[0].exited is True


async def test_stale_resume_falls_back_to_a_fresh_session():
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
        options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
        client_factory=factory,
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


async def test_connect_failure_unrelated_to_resume_propagates():
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
        options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
        client_factory=factory,
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


async def test_the_threads_permission_mode_reaches_the_sdk():
    """/mode writes permission_mode to the store; without this the SDK was only
    ever given the deployment-wide default and the command was a no-op."""
    FakeSdkClient.instances.clear()
    store = MemoryStore()
    chat = FakeChat()
    factory, calls = _client_factory()
    await store.upsert_session("C1", "111.0", permission_mode="plan")

    session = JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        routing=RoutingContext(),
        options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
        client_factory=factory,
    )
    await session.run_turn("hello")

    assert calls[0]["options"]["mode"] == "plan"


async def test_changing_mode_mid_thread_rebuilds_the_client():
    """permission_mode is fixed when the SDK client connects, so a cached client
    would keep the old mode forever -- `/mode bypassPermissions` (the escape
    hatch from approval prompts) would appear to do nothing until the idle sweep
    dropped the session, up to idle_minutes later."""
    FakeSdkClient.instances.clear()
    store = MemoryStore()
    chat = FakeChat()
    factory, calls = _client_factory()

    session = JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        routing=RoutingContext(),
        options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
        client_factory=factory,
    )
    await session.run_turn("hello")
    assert calls[0]["options"]["mode"] is None

    await store.upsert_session("C1", "111.0", permission_mode="bypassPermissions", touch=False)
    await session.run_turn("now hurry up")

    assert len(calls) == 2  # rebuilt rather than reusing the cached client
    assert calls[1]["options"]["mode"] == "bypassPermissions"
    assert calls[1]["options"]["resume"] == "sdk-session-abc"  # same conversation
    assert FakeSdkClient.instances[0].exited is True  # old client closed, not leaked

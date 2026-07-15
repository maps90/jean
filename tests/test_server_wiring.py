"""server.py is the composition root, but nothing in the suite ever built it --
which is how it drifted out of sync with JeanSession's constructor across two
whole tasks with no test noticing. These tests build the small wiring
functions with fakes (no live Slack, no live DB) so a future required kwarg
or a renamed retention setting fails a test instead of only failing at
runtime in the pod.
"""

from __future__ import annotations

import re
from pathlib import Path

from jean import server
from jean.agent_options import build_agent_options
from jean.config import Settings
from jean.maintenance.cleanup import CleanupScheduler
from jean.ports import PruneResult
from jean.session.session import JeanSession


def _make_settings(tmp_path: Path, **overrides: object) -> Settings:
    # Settings() directly, not Settings.load(): .load() only wires the two
    # unprefixed auth env vars in, and takes no other kwargs.
    return Settings(
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        home=tmp_path,
        **overrides,
    )


class FakeChat:
    async def reply(self, channel, thread_ts, text):
        return "999.0"

    async def edit(self, *a, **k):
        raise NotImplementedError

    async def upload(self, *a, **k):
        raise NotImplementedError

    async def react(self, *a, **k):
        raise NotImplementedError

    async def unreact(self, *a, **k):
        raise NotImplementedError

    async def set_status(self, channel, thread_ts, status):
        return None


class FakeStore:
    """Satisfies SessionStore, TranscriptStore, ApprovalCoordinator,
    ThreadLock, and MaintenanceStore structurally -- like PostgresStore, one
    object stands in for all of them. Only the bits build_* exercises are
    implemented; anything else raises loudly if a future test needs it."""

    def __init__(self, *, claim: bool = True) -> None:
        self.claim_calls: list[float] = []
        self.prune_cutoffs: list[tuple[float, float]] = []
        self._claim = claim

    async def get_session(self, channel, thread_ts):
        return None

    async def upsert_session(self, channel, thread_ts, **kwargs):
        return None

    async def bump_turn(self, channel, thread_ts):
        return 1

    async def save(self, channel, thread_ts, sdk_session_id, data):
        return None

    async def load(self, channel, thread_ts, sdk_session_id):
        return None

    async def try_claim_cleanup(self, min_interval: float) -> bool:
        self.claim_calls.append(min_interval)
        return self._claim

    async def prune(self, *, sessions_older_than: float, approvals_older_than: float):
        self.prune_cutoffs.append((sessions_older_than, approvals_older_than))
        return PruneResult(approvals_deleted=0, sessions_deleted=0)


async def _never_asked(tool_name, tool_input, context):
    """build_agent_options requires a permission hook; these tests never run a
    tool, so this stands in for the Slack approval one (build_can_use_tool is
    covered in tests/test_tool_permission.py)."""
    raise AssertionError("the permission hook should not be called here")


def test_build_local_transcripts_matches_agent_cwd(tmp_path):
    """LocalTranscripts derives its directory by slugifying a cwd. That cwd
    MUST be the exact string build_agent_options hands the CLI as `cwd` --
    otherwise the CLI writes transcripts to a project directory jean is not
    looking at, and the whole feature silently does nothing (see the
    docstring on JeanSession._archive)."""
    settings = _make_settings(tmp_path)

    local = server.build_local_transcripts(settings)

    options = build_agent_options(
        persona_text="",
        slack_server=object(),
        slack_tool_names=[],
        mcp_servers={},
        plugins=[],
        settings=settings,
        resume=None,
        can_use_tool=_never_asked,
    )
    expected_slug = re.sub(r"[/.]", "-", options.cwd)
    assert local.path("abc-123") == (
        Path.home() / ".claude" / "projects" / expected_slug / "abc-123.jsonl"
    )


def test_build_session_factory_wires_transcript_store_and_local(tmp_path):
    settings = _make_settings(
        tmp_path,
        transcript_max_mb=7,
        # deliberately NOT the field defaults: the settle wait guards against archiving
        # a turn the CLI has not finished writing, and it must be tunable in production
        settle_timeout=11.0,
        settle_interval=0.25,
        settle_quiet=2.5,
    )
    store = FakeStore()
    local = server.build_local_transcripts(settings)
    bound: list[tuple[str, str]] = []

    def options_factory_for(channel, thread_ts):
        bound.append((channel, thread_ts))

        def options_factory(resume, permission_mode):
            raise NotImplementedError

        return options_factory

    session_factory = server.build_session_factory(
        settings=settings,
        store=store,
        chat=FakeChat(),
        options_factory_for=options_factory_for,
        client_factory=lambda **kwargs: None,
        local_transcripts=local,
    )
    session = session_factory("C1", "111.222")

    assert isinstance(session, JeanSession)
    # The options factory is built PER SESSION, closed over this thread: it carries
    # the SDK permission hook, which must ask for approval in the right thread.
    assert bound == [("C1", "111.222")]
    # White-box: JeanSession exposes no public accessor for its collaborators,
    # so this is the only way to prove the *same* store object was handed in
    # as both SessionStore and TranscriptStore, and that max_transcript_bytes
    # actually came from settings rather than a stale default.
    assert session._transcripts is store
    assert session._local is local
    assert session._max_transcript_bytes == 7 * 1024 * 1024
    assert (session._settle_timeout, session._settle_interval, session._settle_quiet) == (
        11.0,
        0.25,
        2.5,
    )


async def test_build_cleanup_scheduler_uses_settings_retention_and_interval(tmp_path):
    # Deliberately NOT the field defaults (3 / 30 / 24), and deliberately
    # unequal to each other -- so a hardcoded literal, a swap of session<->
    # approval, or an override that silently failed to apply (e.g. because a
    # kwarg no longer matches a renamed Settings field, which pydantic's
    # extra="ignore" would swallow rather than raise) all produce a
    # different, wrong number here instead of accidentally matching.
    settings = _make_settings(
        tmp_path,
        session_retention_days=5,
        approval_retention_days=45,
        cleanup_interval_hours=12,
    )
    store = FakeStore(claim=True)

    scheduler = server.build_cleanup_scheduler(store, settings)
    assert isinstance(scheduler, CleanupScheduler)
    await scheduler.run_once()

    # The interval passed to try_claim_cleanup is the one thing that gates
    # how often a prune can happen at all.
    assert store.claim_calls == [12 * 3600]
    # Both retention windows are derived from the SAME clock reading, so the
    # gap between the two cutoffs is exactly the difference of the two
    # retention windows -- a behavioral assertion that needs no fixed clock
    # and (because the two windows are unequal) would fail if either setting
    # were renamed, hardcoded, or swapped with the other.
    sessions_cutoff, approvals_cutoff = store.prune_cutoffs[0]
    assert approvals_cutoff - sessions_cutoff == (5 * 86400) - (45 * 86400)

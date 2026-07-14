from __future__ import annotations

from contextlib import asynccontextmanager

from jean.ports import (
    ApprovalCoordinator,
    ChatSurface,
    MaintenanceStore,
    PruneResult,
    SessionStore,
    ThreadLock,
    TranscriptStore,
)


class StubStore:
    async def get_session(self, channel, thread_ts):
        return None

    async def upsert_session(
        self,
        channel,
        thread_ts,
        *,
        sdk_session_id=None,
        permission_mode=None,
        touch=True,
    ):
        return None

    async def set_partner(self, channel, thread_ts, user_id):
        return None

    async def get_partner(self, channel, thread_ts):
        return None

    async def bump_turn(self, channel, thread_ts):
        return 1


class StubTranscripts:
    async def save(self, channel, thread_ts, sdk_session_id, data):
        return None

    async def load(self, channel, thread_ts, sdk_session_id):
        return None


class StubMaintenance:
    async def prune(self, *, sessions_older_than, approvals_older_than):
        return PruneResult(approvals_deleted=0, sessions_deleted=0)

    async def try_claim_cleanup(self, min_interval):
        return False


class StubCoordinator:
    async def create(self, approval_id, channel, thread_ts, summary):
        return None

    async def wait(self, approval_id, timeout):
        raise NotImplementedError

    async def resolve(self, approval_id, approved, by):
        return True

    async def set_approvers(self, approval_id, approvers):
        return None

    async def approvers_of(self, approval_id):
        return set()

    async def get_pending(self, approval_id):
        return None


class StubLock:
    def __call__(self, channel, thread_ts):
        return self._cm()

    @asynccontextmanager
    async def _cm(self):
        yield


class StubChat:
    async def reply(self, channel, thread_ts, text):
        return "ts"

    async def edit(self, channel, ts, text):
        return None

    async def upload(
        self, channel, thread_ts, *, path=None, content=None, filename, title=None, comment=None
    ):
        return None

    async def react(self, channel, ts, emoji):
        return None

    async def unreact(self, channel, ts, emoji):
        return None

    async def set_status(self, channel, thread_ts, status):
        return None


def test_stub_store_satisfies_session_store_protocol():
    assert isinstance(StubStore(), SessionStore)


def test_stub_transcripts_satisfies_transcript_store_protocol():
    assert isinstance(StubTranscripts(), TranscriptStore)


def test_stub_maintenance_satisfies_maintenance_store_protocol():
    assert isinstance(StubMaintenance(), MaintenanceStore)


def test_stub_coordinator_satisfies_protocol():
    assert isinstance(StubCoordinator(), ApprovalCoordinator)


def test_stub_lock_satisfies_protocol():
    assert isinstance(StubLock(), ThreadLock)


def test_stub_chat_satisfies_protocol():
    assert isinstance(StubChat(), ChatSurface)

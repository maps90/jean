from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class SessionRow:
    channel: str
    thread_ts: str
    sdk_session_id: str | None
    permission_mode: str | None
    engaged: bool
    last_active_at: float
    turn_seq: int = 0


@dataclass
class ApprovalDecision:
    approved: bool
    by: str


@dataclass
class PruneResult:
    approvals_deleted: int
    sessions_deleted: int


@dataclass
class PluginRef:
    marketplace: str
    plugin: str
    ref: str


@dataclass
class ResolvedPlugin:
    name: str
    path: str


@runtime_checkable
class SessionStore(Protocol):
    async def get_session(self, channel: str, thread_ts: str) -> SessionRow | None: ...
    async def upsert_session(
        self,
        channel: str,
        thread_ts: str,
        *,
        sdk_session_id: str | None = None,
        permission_mode: str | None = None,
        engaged: bool | None = None,
        touch: bool = True,
    ) -> None: ...
    async def set_engaged(self, channel: str, thread_ts: str, value: bool) -> None: ...
    async def is_engaged(self, channel: str, thread_ts: str) -> bool: ...
    async def bump_turn(self, channel: str, thread_ts: str) -> int: ...


@runtime_checkable
class TranscriptStore(Protocol):
    """Durable home for a thread's claude-agent transcript -- the file the CLI
    otherwise keeps only on the pod's local disk. Bytes in, bytes out: any
    compression is the adapter's business, not the domain's.

    `load` returns None unless the stored transcript belongs to `sdk_session_id`;
    handing back another session's transcript would corrupt the thread's memory.
    """

    async def save(
        self, channel: str, thread_ts: str, sdk_session_id: str, data: bytes
    ) -> None: ...
    async def load(self, channel: str, thread_ts: str, sdk_session_id: str) -> bytes | None: ...


@runtime_checkable
class ApprovalCoordinator(Protocol):
    async def create(
        self, approval_id: str, channel: str, thread_ts: str, summary: str
    ) -> None: ...
    async def wait(self, approval_id: str, timeout: float) -> ApprovalDecision: ...
    async def resolve(
        self, approval_id: str, approved: bool, by: str
    ) -> bool: ...  # True if it was pending
    async def set_approvers(self, approval_id: str, approvers: set[str]) -> None: ...
    async def approvers_of(self, approval_id: str) -> set[str]: ...
    async def get_pending(
        self, approval_id: str
    ) -> tuple[str, str, str] | None: ...  # (channel, thread_ts, summary)


@runtime_checkable
class MaintenanceStore(Protocol):
    """Periodic retention cleanup. `prune` deletes rows older than the given
    cutoffs -- sessions and approvals expire on separate schedules, because a
    thread's memory going stale and an audit record aging out are different
    concerns. `try_claim_cleanup` gates a run so exactly one worker prunes each
    period."""

    async def prune(
        self, *, sessions_older_than: float, approvals_older_than: float
    ) -> PruneResult: ...
    async def try_claim_cleanup(self, min_interval: float) -> bool: ...


@runtime_checkable
class ThreadLock(Protocol):
    def __call__(self, channel: str, thread_ts: str) -> AbstractAsyncContextManager[None]: ...


@runtime_checkable
class MarketplaceResolver(Protocol):
    async def resolve(self, entries: list[PluginRef]) -> list[ResolvedPlugin]: ...


@runtime_checkable
class ChatSurface(Protocol):
    async def reply(self, channel: str, thread_ts: str, text: str) -> str: ...
    async def edit(self, channel: str, ts: str, text: str) -> None: ...
    async def upload(
        self,
        channel: str,
        thread_ts: str,
        *,
        path: str | None = None,
        content: str | None = None,
        filename: str,
        title: str | None = None,
        comment: str | None = None,
    ) -> None: ...
    async def react(self, channel: str, ts: str, emoji: str) -> None: ...
    async def unreact(self, channel: str, ts: str, emoji: str) -> None: ...
    async def set_status(self, channel: str, thread_ts: str, status: str) -> None: ...

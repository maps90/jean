from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class SessionRow:
    channel: str
    thread_ts: str
    sdk_session_id: str | None
    permission_mode: str | None
    last_active_at: float
    turn_seq: int = 0
    engaged_with: str | None = None


@dataclass
class ApprovalDecision:
    approved: bool
    by: str
    scope: str = "once"  # "once" | "always"; meaningful only when approved


@dataclass
class PruneResult:
    approvals_deleted: int
    sessions_deleted: int


@dataclass
class Schedule:
    id: str
    channel: str
    thread_ts: str
    cron: str
    timezone: str
    prompt: str
    created_by: str
    # Epoch seconds, like SessionRow.last_active_at. Postgres stores TIMESTAMPTZ
    # and the adapter converts; the domain never handles a datetime.
    next_run_at: float
    last_run_at: float | None = None
    last_status: str | None = None


@dataclass
class PluginRef:
    marketplace: str
    plugin: str
    ref: str
    # Narrow a bundled plugin to these skill names. None = take whatever the
    # marketplace lists. Upstream decides what ships together (anthropics/skills
    # sells 12 skills under one `example-skills` name); a deployment decides what
    # it actually wants in every session's context.
    skills: tuple[str, ...] | None = None


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
        touch: bool = True,
    ) -> None: ...
    async def set_partner(self, channel: str, thread_ts: str, user_id: str | None) -> None: ...
    async def get_partner(self, channel: str, thread_ts: str) -> str | None: ...
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
        self, approval_id: str, approved: bool, by: str, scope: str = "once"
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


@runtime_checkable
class ScheduleStore(Protocol):
    """Recurring prompts, keyed to the thread that owns them.

    `claim_due_schedules` takes an `advance` callback because next_run_at must
    move inside the SAME transaction as the claim -- otherwise a crash between
    claiming and advancing re-fires the schedule on every restart -- and the next
    value is cron math the store cannot do. The callback is pure, so the adapter
    stays testable.
    """

    async def create_schedule(
        self,
        *,
        channel: str,
        thread_ts: str,
        cron: str,
        timezone: str,
        prompt: str,
        created_by: str,
        next_run_at: float,
    ) -> Schedule: ...

    async def list_schedules(self, channel: str, thread_ts: str) -> list[Schedule]: ...

    async def delete_schedule(self, schedule_id: str, channel: str, thread_ts: str) -> bool: ...

    async def claim_due_schedules(
        self, now: float, advance: Callable[[Schedule], float]
    ) -> list[Schedule]: ...

    async def record_run(
        self, schedule_id: str, *, last_run_at: float, last_status: str
    ) -> None: ...


@runtime_checkable
class MetricsSink(Protocol):
    """Per-turn cost and health counters, for the Prometheus scrape.

    Every method is a plain `def`, not `async def` -- the one deliberate
    departure from "async everywhere on I/O paths". A counter increment is an
    in-memory update, not I/O, and `run_turn` emits its turn metric from a
    `finally` block: an await there is a cancellation point, and a cancelled
    bookkeeping call could mask the exception being propagated. Metrics must
    never be able to fail, block, or reorder a turn.

    The `agent` label is NOT a parameter here. Domain code does not know which
    deployment it is; the adapter owns that label (see metrics/prometheus.py).

    `trigger` is "human" | "schedule". `outcome` on a turn is one of
    "ok" | "notice" | "undelivered" | "error" | "rate_limited" -- see
    docs/superpowers/specs/2026-07-30-prometheus-metrics-design.md for what
    each means and the precedence between them.
    """

    def turn_done(self, *, trigger: str, outcome: str, seconds: float) -> None: ...

    def tokens(
        self, *, trigger: str, usage: dict[str, Any] | None, cost_usd: float | None
    ) -> None: ...

    def session_started(self) -> None: ...

    def session_resumed(self, *, outcome: str) -> None: ...  # "ok" | "fresh_fallback"

    def transcript_incomplete(self) -> None: ...

    def schedule_run(self, *, status: str) -> None: ...  # "ok" | "error" | "missed"

    def rate_limit(
        self, *, window: str, utilization: float | None, resets_at: float | None
    ) -> None: ...

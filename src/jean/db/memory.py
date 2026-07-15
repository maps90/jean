from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from jean.ports import ApprovalDecision, PruneResult, SessionRow


@dataclass
class _ApprovalRow:
    channel: str
    thread_ts: str
    summary: str
    approvers: tuple[str, ...] = ()
    future: asyncio.Future = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )
    decision: ApprovalDecision | None = None
    resolved_at: float | None = None


class MemoryStore:
    """In-process implementation of SessionStore + TranscriptStore + ApprovalCoordinator
    + MaintenanceStore + ThreadLock.

    Single-process default and the fast test double. The Postgres adapter
    (db/postgres.py) must match these semantics exactly -- see
    tests/store_behavior.py for the shared assertions both adapters satisfy.
    """

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], SessionRow] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._approvals: dict[str, _ApprovalRow] = {}
        self._transcripts: dict[tuple[str, str], tuple[str, bytes]] = {}
        self._last_cleanup: float | None = None

    # ---- SessionStore ----
    async def get_session(self, channel: str, thread_ts: str) -> SessionRow | None:
        row = self._sessions.get((channel, thread_ts))
        if row is None:
            return None
        return SessionRow(
            channel=row.channel,
            thread_ts=row.thread_ts,
            sdk_session_id=row.sdk_session_id,
            permission_mode=row.permission_mode,
            last_active_at=row.last_active_at,
            turn_seq=row.turn_seq,
            engaged_with=row.engaged_with,
        )

    async def upsert_session(
        self,
        channel: str,
        thread_ts: str,
        *,
        sdk_session_id: str | None = None,
        permission_mode: str | None = None,
        touch: bool = True,
    ) -> None:
        key = (channel, thread_ts)
        existing = self._sessions.get(key)
        row = SessionRow(
            channel=channel,
            thread_ts=thread_ts,
            sdk_session_id=sdk_session_id
            if sdk_session_id is not None
            else (existing.sdk_session_id if existing else None),
            permission_mode=permission_mode
            if permission_mode is not None
            else (existing.permission_mode if existing else None),
            last_active_at=(
                time.time() if touch else (existing.last_active_at if existing else 0.0)
            ),
            turn_seq=existing.turn_seq if existing else 0,
            engaged_with=existing.engaged_with if existing else None,
        )
        self._sessions[key] = row

    async def set_partner(self, channel: str, thread_ts: str, user_id: str | None) -> None:
        # Not routed through upsert_session: `None` there means "leave unchanged",
        # but here it means "clear the partner" -- a real state we must be able to
        # write.
        key = (channel, thread_ts)
        if key not in self._sessions:
            await self.upsert_session(channel, thread_ts, touch=False)
        self._sessions[key].engaged_with = user_id

    async def get_partner(self, channel: str, thread_ts: str) -> str | None:
        row = self._sessions.get((channel, thread_ts))
        return row.engaged_with if row else None

    async def bump_turn(self, channel: str, thread_ts: str) -> int:
        # A newly created row must be touched (last_active_at=now), not left at
        # the zero default -- otherwise it's already older than any retention
        # cutoff and the next prune sweep deletes it before its first real turn.
        # Bumping an *existing* row must not touch last_active_at -- the turn's
        # own upsert_session call already does that.
        key = (channel, thread_ts)
        if key not in self._sessions:
            await self.upsert_session(channel, thread_ts, touch=True)
        row = self._sessions[key]
        row.turn_seq += 1
        return row.turn_seq

    # ---- TranscriptStore ----
    async def save(self, channel: str, thread_ts: str, sdk_session_id: str, data: bytes) -> None:
        # Mirrors Postgres's FK (transcripts.channel,thread_ts -> sessions):
        # a transcript cannot exist without its session row. Postgres enforces
        # this with a ForeignKeyViolationError; here we raise the same
        # *behavior* (see tests/store_behavior.py -- the two adapters differ
        # in exception type, not in whether they raise).
        if (channel, thread_ts) not in self._sessions:
            raise LookupError(
                f"no session for ({channel!r}, {thread_ts!r}) -- call upsert_session() before save()"
            )
        self._transcripts[(channel, thread_ts)] = (sdk_session_id, data)

    async def load(self, channel: str, thread_ts: str, sdk_session_id: str) -> bytes | None:
        stored = self._transcripts.get((channel, thread_ts))
        if stored is None or stored[0] != sdk_session_id:
            return None
        return stored[1]

    # ---- ThreadLock ----
    def __call__(self, channel: str, thread_ts: str):
        return self._lock(channel, thread_ts)

    @asynccontextmanager
    async def _lock(self, channel: str, thread_ts: str):
        key = (channel, thread_ts)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            yield

    # ---- ApprovalCoordinator ----
    async def create(self, approval_id: str, channel: str, thread_ts: str, summary: str) -> None:
        if approval_id in self._approvals:
            return
        self._approvals[approval_id] = _ApprovalRow(
            channel=channel, thread_ts=thread_ts, summary=summary
        )

    async def set_approvers(self, approval_id: str, approvers: set[str]) -> None:
        row = self._approvals.get(approval_id)
        if row is not None:
            row.approvers = tuple(approvers)

    async def approvers_of(self, approval_id: str) -> set[str]:
        row = self._approvals.get(approval_id)
        return set(row.approvers) if row else set()

    async def get_pending(self, approval_id: str) -> tuple[str, str, str] | None:
        row = self._approvals.get(approval_id)
        if row is None:
            return None
        return row.channel, row.thread_ts, row.summary

    async def wait(self, approval_id: str, timeout: float) -> ApprovalDecision:
        row = self._approvals.get(approval_id)
        if row is None:
            # Match PostgresStore: an unknown id has nothing to resolve it, so
            # this blocks for the full timeout before the system-deny, rather
            # than returning immediately -- see tests/store_behavior.py.
            await asyncio.sleep(timeout)
            return ApprovalDecision(False, "system")
        if row.decision is not None:
            return row.decision
        try:
            await asyncio.wait_for(row.future, timeout)
        except TimeoutError:
            decision = ApprovalDecision(False, "system")
            row.decision = decision
            row.resolved_at = time.time()
            if not row.future.done():
                row.future.set_result(decision)
            return decision
        return row.future.result()

    async def resolve(self, approval_id: str, approved: bool, by: str, scope: str = "once") -> bool:
        row = self._approvals.get(approval_id)
        if row is None or row.decision is not None or row.future.done():
            return False
        decision = ApprovalDecision(approved, by, scope)
        row.decision = decision
        row.resolved_at = time.time()
        row.future.set_result(decision)
        return True

    # ---- MaintenanceStore ----
    async def prune(
        self, *, sessions_older_than: float, approvals_older_than: float
    ) -> PruneResult:
        # Resolved approvals (resolved_at set) whose resolution predates the
        # cutoff; pending rows have resolved_at=None and are never pruned.
        stale_appr = [
            aid
            for aid, row in self._approvals.items()
            if row.resolved_at is not None and row.resolved_at < approvals_older_than
        ]
        for aid in stale_appr:
            del self._approvals[aid]
        stale_sess = [
            key for key, row in self._sessions.items() if row.last_active_at < sessions_older_than
        ]
        for key in stale_sess:
            del self._sessions[key]
            # Mirrors the Postgres FK's ON DELETE CASCADE: a session's transcript
            # never outlives the session.
            self._transcripts.pop(key, None)
        return PruneResult(approvals_deleted=len(stale_appr), sessions_deleted=len(stale_sess))

    async def try_claim_cleanup(self, min_interval: float) -> bool:
        # Single process: no cross-worker lock needed, just an interval gate.
        now = time.time()
        if self._last_cleanup is not None and now - self._last_cleanup < min_interval:
            return False
        self._last_cleanup = now
        return True

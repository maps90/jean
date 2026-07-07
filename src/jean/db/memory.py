from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from jean.ports import ApprovalDecision, SessionRow


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


class MemoryStore:
    """In-process implementation of SessionStore + ApprovalCoordinator + ThreadLock.

    Single-process default and the fast test double. The Postgres adapter
    (db/postgres.py) must match these semantics exactly -- see
    tests/store_behavior.py for the shared assertions both adapters satisfy.
    """

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], SessionRow] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._approvals: dict[str, _ApprovalRow] = {}

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
            engaged=row.engaged,
            last_active_at=row.last_active_at,
        )

    async def upsert_session(
        self,
        channel: str,
        thread_ts: str,
        *,
        sdk_session_id: str | None = None,
        permission_mode: str | None = None,
        engaged: bool | None = None,
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
            engaged=engaged if engaged is not None else (existing.engaged if existing else False),
            last_active_at=(
                time.time() if touch else (existing.last_active_at if existing else 0.0)
            ),
        )
        self._sessions[key] = row

    async def set_engaged(self, channel: str, thread_ts: str, value: bool) -> None:
        await self.upsert_session(channel, thread_ts, engaged=value, touch=False)

    async def is_engaged(self, channel: str, thread_ts: str) -> bool:
        row = self._sessions.get((channel, thread_ts))
        return bool(row and row.engaged)

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
            if not row.future.done():
                row.future.set_result(decision)
            return decision
        return row.future.result()

    async def resolve(self, approval_id: str, approved: bool, by: str) -> bool:
        row = self._approvals.get(approval_id)
        if row is None or row.decision is not None or row.future.done():
            return False
        decision = ApprovalDecision(approved, by)
        row.decision = decision
        row.future.set_result(decision)
        return True

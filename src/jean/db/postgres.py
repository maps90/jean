from __future__ import annotations

import asyncio
import gzip
from contextlib import asynccontextmanager

import asyncpg

from jean.ports import ApprovalDecision, PruneResult, SessionRow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  channel text NOT NULL, thread_ts text NOT NULL,
  sdk_session_id text, permission_mode text,
  engaged boolean NOT NULL DEFAULT false,
  last_active_at double precision NOT NULL DEFAULT 0,
  PRIMARY KEY (channel, thread_ts));
CREATE TABLE IF NOT EXISTS approvals (
  id text PRIMARY KEY, channel text NOT NULL, thread_ts text NOT NULL,
  summary text NOT NULL, status text NOT NULL DEFAULT 'pending',
  approved boolean, approver_id text,
  approvers text[] NOT NULL DEFAULT '{}',
  requested_at double precision NOT NULL DEFAULT extract(epoch from now()),
  resolved_at double precision);
CREATE TABLE IF NOT EXISTS maintenance (
  job text PRIMARY KEY, last_run double precision NOT NULL DEFAULT 0);
-- A transcript cannot exist without its session: the FK below means
-- save() raises ForeignKeyViolationError unless a (channel, thread_ts) row
-- already exists in `sessions` -- callers must upsert_session() first.
-- ON DELETE CASCADE means deleting (or pruning) the session takes its
-- transcript with it; see MemoryStore.prune's matching cascade.
CREATE TABLE IF NOT EXISTS transcripts (
  channel text NOT NULL, thread_ts text NOT NULL,
  sdk_session_id text NOT NULL,
  data bytea NOT NULL,
  raw_bytes bigint NOT NULL DEFAULT 0,
  updated_at double precision NOT NULL DEFAULT extract(epoch from now()),
  PRIMARY KEY (channel, thread_ts),
  FOREIGN KEY (channel, thread_ts) REFERENCES sessions(channel, thread_ts) ON DELETE CASCADE);
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS turn_seq bigint NOT NULL DEFAULT 0;
ALTER TABLE transcripts ALTER COLUMN data SET STORAGE EXTERNAL;
"""


class PostgresStore:
    """asyncpg implementation of SessionStore + TranscriptStore + ApprovalCoordinator
    + MaintenanceStore + ThreadLock.

    Must match MemoryStore's semantics exactly -- see tests/store_behavior.py
    for the shared assertions both adapters satisfy. Cross-worker approvals
    use Postgres LISTEN/NOTIFY (channel `jean_approvals`); per-thread
    serialization uses `pg_advisory_xact_lock`.
    """

    def __init__(self, pool: asyncpg.Pool, dsn: str):
        self._pool = pool
        self._dsn = dsn

    @classmethod
    async def connect(cls, dsn: str, *, min_size: int = 1, max_size: int = 5) -> PostgresStore:
        """Open the pool. Sizes come from Settings (`JEAN_DB_POOL_MIN/MAX`) so a
        deployment sharing a small managed Postgres can shrink its footprint
        without a rebuild -- see the note on Settings.db_pool_max."""
        pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
        async with pool.acquire() as c:
            await c.execute(_SCHEMA)
        return cls(pool, dsn)

    async def ping(self) -> bool:
        async with self._pool.acquire() as c:
            return (await c.fetchval("SELECT 1")) == 1

    async def close(self) -> None:
        await self._pool.close()

    # ---- SessionStore ----
    async def get_session(self, channel: str, thread_ts: str) -> SessionRow | None:
        r = await self._pool.fetchrow(
            "SELECT * FROM sessions WHERE channel=$1 AND thread_ts=$2", channel, thread_ts
        )
        return (
            None
            if r is None
            else SessionRow(
                channel=r["channel"],
                thread_ts=r["thread_ts"],
                sdk_session_id=r["sdk_session_id"],
                permission_mode=r["permission_mode"],
                engaged=r["engaged"],
                last_active_at=r["last_active_at"],
                turn_seq=r["turn_seq"],
            )
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
        # COALESCE keeps existing values when a field is not being changed.
        await self._pool.execute(
            """INSERT INTO sessions(channel,thread_ts,sdk_session_id,permission_mode,engaged,last_active_at)
               VALUES($1,$2,$3,$4,COALESCE($5,false),
                      CASE WHEN $6 THEN extract(epoch from now()) ELSE 0 END)
               ON CONFLICT(channel,thread_ts) DO UPDATE SET
                 sdk_session_id=COALESCE($3, sessions.sdk_session_id),
                 permission_mode=COALESCE($4, sessions.permission_mode),
                 engaged=COALESCE($5, sessions.engaged),
                 last_active_at=CASE WHEN $6 THEN extract(epoch from now()) ELSE sessions.last_active_at END""",
            channel,
            thread_ts,
            sdk_session_id,
            permission_mode,
            engaged,
            touch,
        )

    async def set_engaged(self, channel: str, thread_ts: str, value: bool) -> None:
        await self.upsert_session(channel, thread_ts, engaged=value, touch=False)

    async def is_engaged(self, channel: str, thread_ts: str) -> bool:
        v = await self._pool.fetchval(
            "SELECT engaged FROM sessions WHERE channel=$1 AND thread_ts=$2", channel, thread_ts
        )
        return bool(v)

    async def bump_turn(self, channel: str, thread_ts: str) -> int:
        # INSERT..ON CONFLICT so a bump on a thread with no row yet still works;
        # RETURNING hands back the new value the caller must remember. A newly
        # inserted row is touched (last_active_at=now) so it isn't already
        # older than every retention cutoff before its first real turn; the
        # conflict path leaves last_active_at alone -- the turn's own
        # upsert_session call already touches it.
        return await self._pool.fetchval(
            """INSERT INTO sessions(channel,thread_ts,turn_seq,last_active_at)
               VALUES($1,$2,1,extract(epoch from now()))
               ON CONFLICT(channel,thread_ts) DO UPDATE SET turn_seq=sessions.turn_seq+1
               RETURNING turn_seq""",
            channel,
            thread_ts,
        )

    # ---- TranscriptStore ----  gzip is a storage detail; the port speaks raw bytes.
    #
    # gzip is CPU-bound and this is a single-threaded event loop shared by every
    # thread on the worker (and the health server), so it runs on a thread via
    # asyncio.to_thread -- at the 32 MB cap (JEAN_TRANSCRIPT_MAX_MB) compression
    # takes ~0.7s, which on the loop would stall every other Slack thread's turn
    # and the readiness probe with it. Level 6 rather than gzip's default 9: 9
    # buys a few percent on already-repetitive JSONL for several times the CPU.
    async def save(self, channel: str, thread_ts: str, sdk_session_id: str, data: bytes) -> None:
        blob = await asyncio.to_thread(gzip.compress, data, compresslevel=6)
        await self._pool.execute(
            """INSERT INTO transcripts(channel,thread_ts,sdk_session_id,data,raw_bytes,updated_at)
               VALUES($1,$2,$3,$4,$5,extract(epoch from now()))
               ON CONFLICT(channel,thread_ts) DO UPDATE SET
                 sdk_session_id=$3, data=$4, raw_bytes=$5,
                 updated_at=extract(epoch from now())""",
            channel,
            thread_ts,
            sdk_session_id,
            blob,
            len(data),
        )

    async def load(self, channel: str, thread_ts: str, sdk_session_id: str) -> bytes | None:
        r = await self._pool.fetchrow(
            "SELECT sdk_session_id, data FROM transcripts WHERE channel=$1 AND thread_ts=$2",
            channel,
            thread_ts,
        )
        if r is None or r["sdk_session_id"] != sdk_session_id:
            return None
        return await asyncio.to_thread(gzip.decompress, r["data"])

    # ---- ThreadLock ----  advisory *xact* lock auto-releases on tx end.
    # This is held for the ENTIRE agent turn (per-thread serialization across
    # workers), so it must NOT be drawn from the shared query pool -- doing so
    # would starve short queries (upsert_session/is_engaged/...) and, worse,
    # ApprovalCoordinator.wait()'s own pool.acquire() once enough threads are
    # mid-turn (a pool-exhaustion deadlock). Use a dedicated connection instead.
    def __call__(self, channel: str, thread_ts: str):
        return self._lock(channel, thread_ts)

    @asynccontextmanager
    async def _lock(self, channel: str, thread_ts: str):
        key = f"{channel}:{thread_ts}"
        conn = await asyncpg.connect(self._dsn)
        try:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", key)
                yield
        finally:
            await conn.close()

    # ---- ApprovalCoordinator ----  cross-worker via LISTEN/NOTIFY
    async def create(self, approval_id: str, channel: str, thread_ts: str, summary: str) -> None:
        await self._pool.execute(
            "INSERT INTO approvals(id,channel,thread_ts,summary) VALUES($1,$2,$3,$4) "
            "ON CONFLICT(id) DO NOTHING",
            approval_id,
            channel,
            thread_ts,
            summary,
        )

    async def set_approvers(self, approval_id: str, approvers: set[str]) -> None:
        await self._pool.execute(
            "UPDATE approvals SET approvers=$2 WHERE id=$1", approval_id, list(approvers)
        )

    async def approvers_of(self, approval_id: str) -> set[str]:
        v = await self._pool.fetchval("SELECT approvers FROM approvals WHERE id=$1", approval_id)
        return set(v) if v else set()

    async def get_pending(self, approval_id: str) -> tuple[str, str, str] | None:
        r = await self._pool.fetchrow(
            "SELECT channel,thread_ts,summary FROM approvals WHERE id=$1", approval_id
        )
        return None if r is None else (r["channel"], r["thread_ts"], r["summary"])

    async def wait(self, approval_id: str, timeout: float) -> ApprovalDecision:
        # Dedicated (non-pooled) connection LISTENs for up to `timeout` (the
        # approval_ttl, e.g. 1800s); a NOTIFY (or an already-resolved row)
        # wakes us. This must NOT be a pool.acquire() -- it would hold a
        # pooled connection for the whole wait, competing with the lock
        # connection and short queries for the same limited pool.
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        conn = await asyncpg.connect(self._dsn)
        try:

            def _cb(_c, _pid, _chan, payload):
                if payload == approval_id and not fut.done():
                    fut.set_result(True)

            await conn.add_listener("jean_approvals", _cb)
            row = await conn.fetchrow(
                "SELECT status,approved,approver_id FROM approvals WHERE id=$1", approval_id
            )
            if row and row["status"] != "pending":
                return ApprovalDecision(bool(row["approved"]), row["approver_id"] or "unknown")
            try:
                await asyncio.wait_for(fut, timeout)
            except TimeoutError:
                await self.resolve(approval_id, False, "system")
            row = await conn.fetchrow(
                "SELECT approved,approver_id FROM approvals WHERE id=$1", approval_id
            )
            return ApprovalDecision(
                bool(row["approved"]) if row else False,
                (row["approver_id"] if row else None) or "system",
            )
        finally:
            await conn.remove_listener("jean_approvals", _cb)
            await conn.close()

    async def resolve(self, approval_id: str, approved: bool, by: str) -> bool:
        status = await self._pool.fetchval(
            "UPDATE approvals SET status=$2, approved=$3, approver_id=$4, resolved_at=extract(epoch from now()) "
            "WHERE id=$1 AND status='pending' RETURNING id",
            approval_id,
            "approved" if approved else "denied",
            approved,
            by,
        )
        if status is None:
            return False
        await self._pool.execute("SELECT pg_notify('jean_approvals', $1)", approval_id)
        return True

    # ---- MaintenanceStore ----  retention cleanup, swept every cleanup_interval_hours
    async def prune(
        self, *, sessions_older_than: float, approvals_older_than: float
    ) -> PruneResult:
        # One transaction so the two deletes commit together. Resolved
        # approvals carry resolved_at; pending rows have it NULL and are never
        # matched (NULL < cutoff is NULL) -- mirrors MemoryStore exactly.
        async with self._pool.acquire() as c, c.transaction():
            appr = await c.execute(
                "DELETE FROM approvals WHERE resolved_at IS NOT NULL AND resolved_at < $1",
                approvals_older_than,
            )
            sess = await c.execute(
                "DELETE FROM sessions WHERE last_active_at < $1", sessions_older_than
            )
        # asyncpg returns a command tag like "DELETE 3"; the count is the tail.
        return PruneResult(
            approvals_deleted=int(appr.split()[-1]),
            sessions_deleted=int(sess.split()[-1]),
        )

    async def try_claim_cleanup(self, min_interval: float) -> bool:
        # Cross-worker gate: a non-blocking advisory *xact* lock means only one
        # worker holds the claim at a time (the rest return False and skip),
        # and the durable maintenance.last_run row enforces the interval so a
        # restart doesn't re-run early. Lock auto-releases on commit.
        async with self._pool.acquire() as c, c.transaction():
            got = await c.fetchval("SELECT pg_try_advisory_xact_lock(hashtext('jean_cleanup'))")
            if not got:
                return False
            await c.execute("INSERT INTO maintenance(job) VALUES('cleanup') ON CONFLICT DO NOTHING")
            due = await c.fetchval(
                "SELECT extract(epoch from now()) - last_run >= $1 "
                "FROM maintenance WHERE job='cleanup'",
                min_interval,
            )
            if not due:
                return False
            await c.execute(
                "UPDATE maintenance SET last_run=extract(epoch from now()) WHERE job='cleanup'"
            )
            return True

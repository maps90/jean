# Scheduled Prompts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a human ask an agent for a recurring prompt ("sprint summary every Tuesday morning") and have a worker inject that prompt as an ordinary turn in the originating thread when it comes due.

**Architecture:** A `schedules` table holds a cron expression, a timezone, a prompt, and the thread it belongs to. A `ScheduleRunner` polls, claims due rows with `FOR UPDATE SKIP LOCKED`, and calls `SessionManager.handle()` — which already takes the per-thread lock and sits below the gateway, so engagement state is untouched. Creating and removing go through the existing `ApprovalGate`.

**Tech Stack:** Python 3.11+, asyncpg, pydantic-settings, claude-agent-sdk in-process MCP, pytest + pytest-asyncio (`asyncio_mode = "auto"`), croniter (new).

Spec: `docs/superpowers/specs/2026-07-29-scheduled-prompts-design.md`

## Global Constraints

- `from __future__ import annotations` at the top of every module; modern hints (`str | None`, `list[str]`).
- Async on every I/O path. Domain methods touching a port are `async`.
- Dependency injection only. No module-level singletons for stateful things.
- Layering: nothing in `schedule/` may import `asyncpg`, `slack_bolt`, or `slack_sdk`. Only `db/` and `server.py` touch concrete infra.
- Config via `JEAN_*` env → `Settings` (pydantic-settings, `env_prefix="JEAN_"`).
- Tests use fakes at the ports. No live network. Test output must be pristine — no warnings.
- Postgres-backed tests are skipped unless `JEAN_TEST_DATABASE_URL` is set. The default `uv run pytest` must need no database.
- Run `./scripts/verify.sh` before every commit (ruff check + ruff format-check + pytest).
- Do NOT add AI co-author trailers to commits.
- Never name the company or its internal systems in code, comments, tests, docs, or commit messages.
- Time crossing a port boundary is **epoch seconds as `float`**, matching `SessionRow.last_active_at`. Postgres stores `TIMESTAMPTZ`; the adapter converts.

---

### Task 1: Cron helpers

Pure time math, isolated so DST and rollover cases are testable with no store, no clock, no I/O.

**Files:**
- Create: `src/jean/schedule/__init__.py` (empty)
- Create: `src/jean/schedule/cron.py`
- Modify: `pyproject.toml` (add `croniter` to `dependencies`)
- Test: `tests/test_schedule_cron.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `CronError(ValueError)`
  - `validate(cron: str, timezone: str) -> None` — raises `CronError`
  - `next_after(cron: str, timezone: str, after: float) -> float` — epoch in, epoch out

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, add `"croniter>=2.0"` to the `dependencies` list. Then:

```bash
uv sync
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_schedule_cron.py`:

```python
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from jean.schedule.cron import CronError, next_after, validate


def _epoch(y: int, m: int, d: int, hh: int, mm: int, tz: str) -> float:
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(tz)).timestamp()


def test_next_after_advances_to_the_next_weekly_occurrence() -> None:
    # Tuesday 09:00 in Asia/Jakarta, asked from Tuesday 09:00 exactly.
    after = _epoch(2026, 8, 4, 9, 0, "Asia/Jakarta")
    got = next_after("0 9 * * 2", "Asia/Jakarta", after)
    assert got == _epoch(2026, 8, 11, 9, 0, "Asia/Jakarta")


def test_next_after_is_strictly_after_the_given_instant() -> None:
    # A run that fires at its due time must not compute itself as next.
    after = _epoch(2026, 8, 4, 9, 0, "Asia/Jakarta")
    assert next_after("0 9 * * 2", "Asia/Jakarta", after) > after


def test_next_after_crosses_a_month_boundary() -> None:
    after = _epoch(2026, 8, 31, 23, 0, "UTC")
    got = next_after("0 9 * * *", "UTC", after)
    assert got == _epoch(2026, 9, 1, 9, 0, "UTC")


def test_next_after_keeps_local_wall_clock_across_a_dst_shift() -> None:
    # Europe/London moves off BST on 2026-10-25. A 09:00 daily schedule must
    # still be 09:00 local the next morning, which is a different UTC offset --
    # this is the case naive epoch arithmetic gets wrong.
    after = _epoch(2026, 10, 24, 12, 0, "Europe/London")
    got = next_after("0 9 * * *", "Europe/London", after)
    local = datetime.fromtimestamp(got, ZoneInfo("Europe/London"))
    assert (local.year, local.month, local.day) == (2026, 10, 25)
    assert (local.hour, local.minute) == (9, 0)


def test_validate_rejects_a_malformed_expression() -> None:
    with pytest.raises(CronError):
        validate("not a cron", "UTC")


def test_validate_rejects_an_unknown_timezone() -> None:
    with pytest.raises(CronError):
        validate("0 9 * * 2", "Mars/Olympus_Mons")


def test_validate_accepts_a_good_expression() -> None:
    validate("0 9 * * 2", "Asia/Jakarta")
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_schedule_cron.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jean.schedule'`

- [ ] **Step 4: Write the implementation**

Create `src/jean/schedule/__init__.py` as an empty file.

Create `src/jean/schedule/cron.py`:

```python
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter


class CronError(ValueError):
    """A cron expression or timezone that cannot be scheduled."""


def _zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CronError(f"unknown timezone {timezone!r}") from exc


def validate(cron: str, timezone: str) -> None:
    """Raise CronError unless this pair can produce a next occurrence.

    Called before an approval is raised, so nobody is ever asked to approve a
    schedule that cannot run.
    """
    zone = _zone(timezone)
    if not croniter.is_valid(cron):
        raise CronError(f"invalid cron expression {cron!r}")
    # is_valid accepts field shapes that still cannot yield an occurrence
    # (e.g. Feb 30). Force one to prove the pair is schedulable.
    try:
        croniter(cron, datetime.now(zone)).get_next(datetime)
    except (ValueError, KeyError) as exc:
        raise CronError(f"cron {cron!r} yields no occurrence") from exc


def next_after(cron: str, timezone: str, after: float) -> float:
    """Epoch of the first occurrence strictly after `after`.

    Computed in local time, not UTC: "09:00 Tuesday" means 09:00 on the wall
    clock, which is a different UTC offset either side of a DST shift. croniter
    is given a zone-aware datetime so it advances local wall-clock fields and we
    convert back at the end.
    """
    zone = _zone(timezone)
    if not croniter.is_valid(cron):
        raise CronError(f"invalid cron expression {cron!r}")
    base = datetime.fromtimestamp(after, zone)
    return float(croniter(cron, base).get_next(datetime).timestamp())
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_schedule_cron.py -v`
Expected: PASS (7 tests)

- [ ] **Step 6: Verify and commit**

```bash
./scripts/verify.sh
git add pyproject.toml uv.lock src/jean/schedule/__init__.py src/jean/schedule/cron.py tests/test_schedule_cron.py
git commit -m "feat(schedule): cron helpers with local wall-clock semantics"
```

---

### Task 2: ScheduleStore port and in-memory adapter

**Files:**
- Modify: `src/jean/ports.py` (add `Schedule` dataclass and `ScheduleStore` protocol)
- Modify: `src/jean/db/memory.py` (add methods to `MemoryStore`)
- Modify: `tests/store_behavior.py` (shared assertions, so both adapters are proven identical)
- Modify: `tests/test_memory_store.py` (call the new assertions)

**Interfaces:**
- Consumes: nothing from Task 1 at runtime.
- Produces:
  - `Schedule` dataclass: `id, channel, thread_ts, cron, timezone, prompt, created_by, next_run_at: float, last_run_at: float | None, last_status: str | None`
  - `ScheduleStore` protocol:
    - `create_schedule(*, channel, thread_ts, cron, timezone, prompt, created_by, next_run_at) -> Schedule`
    - `list_schedules(channel, thread_ts) -> list[Schedule]`
    - `delete_schedule(schedule_id, channel, thread_ts) -> bool`
    - `claim_due_schedules(now: float, advance: Callable[[Schedule], float]) -> list[Schedule]`
    - `record_run(schedule_id, *, last_run_at: float, last_status: str) -> None`

**Why `claim_due_schedules` takes a callback:** the spec requires `next_run_at` to advance in the *same transaction* as the claim, and the next value is cron math the store cannot do. The adapter opens a transaction, selects due rows with `FOR UPDATE SKIP LOCKED`, calls `advance(row)` per row, writes the result, and commits. The callback is pure, so this stays testable.

- [ ] **Step 1: Write the failing shared assertions**

Add to `tests/store_behavior.py`:

```python
async def assert_schedule_crud(store) -> None:
    channel, thread_ts = "C1", "111.222"
    assert await store.list_schedules(channel, thread_ts) == []

    made = await store.create_schedule(
        channel=channel,
        thread_ts=thread_ts,
        cron="0 9 * * 2",
        timezone="Asia/Jakarta",
        prompt="post the weekly sprint summary",
        created_by="U1",
        next_run_at=1000.0,
    )
    assert made.id
    assert made.cron == "0 9 * * 2"
    assert made.next_run_at == 1000.0
    assert made.last_status is None

    rows = await store.list_schedules(channel, thread_ts)
    assert [r.id for r in rows] == [made.id]

    # Scoped to the thread: another thread sees nothing.
    assert await store.list_schedules(channel, "999.999") == []

    # And cannot delete it either.
    assert await store.delete_schedule(made.id, channel, "999.999") is False
    assert await store.delete_schedule(made.id, channel, thread_ts) is True
    assert await store.delete_schedule(made.id, channel, thread_ts) is False
    assert await store.list_schedules(channel, thread_ts) == []


async def assert_schedule_claim_advances_and_is_exclusive(store) -> None:
    made = await store.create_schedule(
        channel="C1",
        thread_ts="111.222",
        cron="0 9 * * 2",
        timezone="UTC",
        prompt="p",
        created_by="U1",
        next_run_at=1000.0,
    )

    # Nothing due yet.
    assert await store.claim_due_schedules(999.0, lambda s: 2000.0) == []

    claimed = await store.claim_due_schedules(1000.0, lambda s: 2000.0)
    assert [c.id for c in claimed] == [made.id]
    # The claimed row carries the DUE time, not the advanced one -- the runner
    # needs the original to decide whether it is inside the grace window.
    assert claimed[0].next_run_at == 1000.0

    # Advanced in the same call, so a second claimer at the same instant sees
    # nothing. This is what stops two workers firing one schedule.
    assert await store.claim_due_schedules(1000.0, lambda s: 2000.0) == []
    assert await store.claim_due_schedules(2000.0, lambda s: 3000.0) != []


async def assert_schedule_record_run(store) -> None:
    made = await store.create_schedule(
        channel="C1",
        thread_ts="111.222",
        cron="0 9 * * 2",
        timezone="UTC",
        prompt="p",
        created_by="U1",
        next_run_at=1000.0,
    )
    await store.record_run(made.id, last_run_at=1234.0, last_status="ok")
    row = (await store.list_schedules("C1", "111.222"))[0]
    assert row.last_run_at == 1234.0
    assert row.last_status == "ok"
```

In `tests/test_memory_store.py`, add these three names to the existing
`from tests.store_behavior import (...)` block (it imports each helper by name,
alphabetically):

```python
    assert_schedule_claim_advances_and_is_exclusive,
    assert_schedule_crud,
    assert_schedule_record_run,
```

Then add the tests:

```python
async def test_schedule_crud() -> None:
    await assert_schedule_crud(MemoryStore())


async def test_schedule_claim_advances_and_is_exclusive() -> None:
    await assert_schedule_claim_advances_and_is_exclusive(MemoryStore())


async def test_schedule_record_run() -> None:
    await assert_schedule_record_run(MemoryStore())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_memory_store.py -k schedule -v`
Expected: FAIL — `AttributeError: 'MemoryStore' object has no attribute 'list_schedules'`

- [ ] **Step 3: Add the port**

In `src/jean/ports.py`, add the dataclass next to the other dataclasses:

```python
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
```

And the protocol, alongside the other protocols:

```python
@runtime_checkable
class ScheduleStore(Protocol):
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
```

Add `from collections.abc import Callable` to the imports at the top of `ports.py` if it is not already there.

- [ ] **Step 4: Implement on MemoryStore**

In `src/jean/db/memory.py`, add `import uuid` and `from jean.ports import Schedule` to the imports, initialise `self._schedules: dict[str, Schedule] = {}` in `__init__`, and add:

```python
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
    ) -> Schedule:
        row = Schedule(
            id=uuid.uuid4().hex,
            channel=channel,
            thread_ts=thread_ts,
            cron=cron,
            timezone=timezone,
            prompt=prompt,
            created_by=created_by,
            next_run_at=next_run_at,
        )
        self._schedules[row.id] = row
        return row

    async def list_schedules(self, channel: str, thread_ts: str) -> list[Schedule]:
        return [
            s
            for s in self._schedules.values()
            if s.channel == channel and s.thread_ts == thread_ts
        ]

    async def delete_schedule(self, schedule_id: str, channel: str, thread_ts: str) -> bool:
        row = self._schedules.get(schedule_id)
        # Thread-scoped on purpose: an id from another thread is "not found", so
        # one thread cannot cancel another's schedules.
        if row is None or row.channel != channel or row.thread_ts != thread_ts:
            return False
        del self._schedules[schedule_id]
        return True

    async def claim_due_schedules(
        self, now: float, advance: Callable[[Schedule], float]
    ) -> list[Schedule]:
        claimed: list[Schedule] = []
        for row in self._schedules.values():
            if row.next_run_at > now:
                continue
            due = replace(row)  # snapshot carrying the DUE time
            row.next_run_at = advance(row)
            claimed.append(due)
        return claimed

    async def record_run(
        self, schedule_id: str, *, last_run_at: float, last_status: str
    ) -> None:
        row = self._schedules.get(schedule_id)
        if row is None:
            return
        row.last_run_at = last_run_at
        row.last_status = last_status
```

Add `replace` to the existing `from dataclasses import ...` line in `memory.py`, and `Callable` to its typing imports.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_memory_store.py -k schedule -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Verify and commit**

```bash
./scripts/verify.sh
git add src/jean/ports.py src/jean/db/memory.py tests/store_behavior.py tests/test_memory_store.py
git commit -m "feat(schedule): ScheduleStore port and in-memory adapter"
```

---

### Task 3: Postgres adapter

**Files:**
- Modify: `src/jean/db/postgres.py` (`_SCHEMA` and `PostgresStore`)
- Modify: `tests/test_postgres_store.py` (call the same three shared assertions)

**Interfaces:**
- Consumes: `Schedule`, `ScheduleStore` from Task 2.
- Produces: nothing new — `PostgresStore` satisfies the same protocol, proven by the same assertions.

- [ ] **Step 1: Write the failing tests**

`tests/test_postgres_store.py` already has a module-level
`pytestmark = pytest.mark.skipif(not os.environ.get("JEAN_TEST_DATABASE_URL"), …)`
and an async `store` fixture (line 36), so these skip automatically without a
database. Add the same three names to its
`from tests.store_behavior import (  # noqa: E402` block, then:

```python
async def test_schedule_crud(store):
    await assert_schedule_crud(store)


async def test_schedule_claim_advances_and_is_exclusive(store):
    await assert_schedule_claim_advances_and_is_exclusive(store)


async def test_schedule_record_run(store):
    await assert_schedule_record_run(store)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `JEAN_TEST_DATABASE_URL=postgresql://... uv run pytest tests/test_postgres_store.py -k schedule -v`
Expected: FAIL — `asyncpg.exceptions.UndefinedTableError: relation "schedules" does not exist`

Without `JEAN_TEST_DATABASE_URL` these skip. That is correct — confirm they *skip* rather than pass, then set the variable to actually see the failure.

- [ ] **Step 3: Add the table**

Append to `_SCHEMA` in `src/jean/db/postgres.py`:

```sql
CREATE TABLE IF NOT EXISTS schedules (
  id           TEXT PRIMARY KEY,
  channel      TEXT NOT NULL,
  thread_ts    TEXT NOT NULL,
  cron         TEXT NOT NULL,
  timezone     TEXT NOT NULL,
  prompt       TEXT NOT NULL,
  created_by   TEXT NOT NULL,
  next_run_at  TIMESTAMPTZ NOT NULL,
  last_run_at  TIMESTAMPTZ,
  last_status  TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS schedules_due ON schedules (next_run_at);
```

No foreign key to `sessions`: retention prunes sessions after a few days and a weekly schedule must outlive that. A cascade would delete schedules during routine cleanup.

- [ ] **Step 4: Implement on PostgresStore**

Add `import uuid` and `from datetime import UTC, datetime` to the imports if absent, plus a module-level helper and the methods:

```python
def _epoch(value: datetime | None) -> float | None:
    return None if value is None else value.timestamp()


def _row_to_schedule(r: asyncpg.Record) -> Schedule:
    return Schedule(
        id=r["id"],
        channel=r["channel"],
        thread_ts=r["thread_ts"],
        cron=r["cron"],
        timezone=r["timezone"],
        prompt=r["prompt"],
        created_by=r["created_by"],
        next_run_at=r["next_run_at"].timestamp(),
        last_run_at=_epoch(r["last_run_at"]),
        last_status=r["last_status"],
    )
```

```python
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
    ) -> Schedule:
        schedule_id = uuid.uuid4().hex
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO schedules
                     (id, channel, thread_ts, cron, timezone, prompt, created_by, next_run_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                   RETURNING *""",
                schedule_id,
                channel,
                thread_ts,
                cron,
                timezone,
                prompt,
                created_by,
                datetime.fromtimestamp(next_run_at, UTC),
            )
        return _row_to_schedule(row)

    async def list_schedules(self, channel: str, thread_ts: str) -> list[Schedule]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM schedules WHERE channel = $1 AND thread_ts = $2 "
                "ORDER BY created_at",
                channel,
                thread_ts,
            )
        return [_row_to_schedule(r) for r in rows]

    async def delete_schedule(self, schedule_id: str, channel: str, thread_ts: str) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM schedules WHERE id = $1 AND channel = $2 AND thread_ts = $3",
                schedule_id,
                channel,
                thread_ts,
            )
        return result != "DELETE 0"

    async def claim_due_schedules(
        self, now: float, advance: Callable[[Schedule], float]
    ) -> list[Schedule]:
        cutoff = datetime.fromtimestamp(now, UTC)
        claimed: list[Schedule] = []
        async with self._pool.acquire() as conn, conn.transaction():
            # SKIP LOCKED, not a global claim gate: schedules are independent, so
            # workers share the load and two of them cannot take the same row.
            rows = await conn.fetch(
                "SELECT * FROM schedules WHERE next_run_at <= $1 "
                "FOR UPDATE SKIP LOCKED",
                cutoff,
            )
            for r in rows:
                due = _row_to_schedule(r)
                # Advance inside this transaction, before the turn runs: a worker
                # that dies mid-turn loses one firing rather than re-firing on
                # every restart.
                await conn.execute(
                    "UPDATE schedules SET next_run_at = $2 WHERE id = $1",
                    due.id,
                    datetime.fromtimestamp(advance(due), UTC),
                )
                claimed.append(due)
        return claimed

    async def record_run(
        self, schedule_id: str, *, last_run_at: float, last_status: str
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE schedules SET last_run_at = $2, last_status = $3 WHERE id = $1",
                schedule_id,
                datetime.fromtimestamp(last_run_at, UTC),
                last_status,
            )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `JEAN_TEST_DATABASE_URL=postgresql://... uv run pytest tests/test_postgres_store.py -k schedule -v`
Expected: PASS (3 tests)

Then confirm the default run still needs no database:

Run: `uv run pytest tests/test_postgres_store.py -k schedule -v`
Expected: 3 skipped

- [ ] **Step 6: Verify and commit**

```bash
./scripts/verify.sh
git add src/jean/db/postgres.py tests/test_postgres_store.py
git commit -m "feat(schedule): Postgres adapter with SKIP LOCKED claiming"
```

---

### Task 4: ScheduleRunner

**Files:**
- Create: `src/jean/schedule/runner.py`
- Test: `tests/test_schedule_runner.py`

**Interfaces:**
- Consumes: `ScheduleStore`, `Schedule` (Task 2); `next_after` (Task 1).
- Produces:
  - `ScheduleRunner(store, handle, *, grace_seconds=3600.0, poll_seconds=30.0, clock=time.time)`
    where `handle: Callable[[str, str, str], Awaitable[None]]` — matches `SessionManager.handle(channel, thread_ts, text)`.
  - `async run_once() -> int` — number of schedules actually fired.
  - `async run() -> None` — the background loop.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_schedule_runner.py`:

```python
from __future__ import annotations

from jean.db.memory import MemoryStore
from jean.schedule.runner import ScheduleRunner


class FakeHandle:
    """Stands in for SessionManager.handle."""

    def __init__(self, raises: bool = False) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._raises = raises

    async def __call__(self, channel: str, thread_ts: str, text: str) -> None:
        self.calls.append((channel, thread_ts, text))
        if self._raises:
            raise RuntimeError("turn blew up")


async def _make(store: MemoryStore, next_run_at: float) -> str:
    row = await store.create_schedule(
        channel="C1",
        thread_ts="111.222",
        cron="0 9 * * 2",
        timezone="UTC",
        prompt="post the weekly sprint summary",
        created_by="U1",
        next_run_at=next_run_at,
    )
    return row.id


async def test_fires_a_due_schedule_as_a_turn_in_its_thread() -> None:
    store = MemoryStore()
    await _make(store, 1000.0)
    handle = FakeHandle()
    runner = ScheduleRunner(store, handle, clock=lambda: 1000.0)

    assert await runner.run_once() == 1
    assert handle.calls == [("C1", "111.222", "post the weekly sprint summary")]


async def test_does_not_fire_before_it_is_due() -> None:
    store = MemoryStore()
    await _make(store, 1000.0)
    handle = FakeHandle()
    runner = ScheduleRunner(store, handle, clock=lambda: 999.0)

    assert await runner.run_once() == 0
    assert handle.calls == []


async def test_fires_late_inside_the_grace_window() -> None:
    store = MemoryStore()
    await _make(store, 1000.0)
    handle = FakeHandle()
    runner = ScheduleRunner(store, handle, grace_seconds=3600.0, clock=lambda: 1000.0 + 1800)

    assert await runner.run_once() == 1
    assert len(handle.calls) == 1


async def test_skips_and_marks_missed_past_the_grace_window() -> None:
    # A summary posted two days late carries a "weekly" framing that is no
    # longer true. Record the miss rather than posting it.
    store = MemoryStore()
    schedule_id = await _make(store, 1000.0)
    handle = FakeHandle()
    runner = ScheduleRunner(store, handle, grace_seconds=3600.0, clock=lambda: 1000.0 + 7200)

    assert await runner.run_once() == 0
    assert handle.calls == []
    row = (await store.list_schedules("C1", "111.222"))[0]
    assert row.id == schedule_id
    assert row.last_status == "missed"


async def test_advances_next_run_at_so_it_does_not_refire() -> None:
    store = MemoryStore()
    await _make(store, 1000.0)
    handle = FakeHandle()
    runner = ScheduleRunner(store, handle, clock=lambda: 1000.0)

    assert await runner.run_once() == 1
    assert await runner.run_once() == 0
    assert len(handle.calls) == 1


async def test_records_ok_after_a_successful_firing() -> None:
    store = MemoryStore()
    await _make(store, 1000.0)
    runner = ScheduleRunner(store, FakeHandle(), clock=lambda: 1000.0)

    await runner.run_once()

    row = (await store.list_schedules("C1", "111.222"))[0]
    assert row.last_status == "ok"
    assert row.last_run_at == 1000.0


async def test_a_failing_turn_is_recorded_and_does_not_stop_the_others() -> None:
    store = MemoryStore()
    await _make(store, 1000.0)
    runner = ScheduleRunner(store, FakeHandle(raises=True), clock=lambda: 1000.0)

    # run_once must not propagate: one bad schedule cannot kill the loop.
    assert await runner.run_once() == 0

    row = (await store.list_schedules("C1", "111.222"))[0]
    assert row.last_status == "error"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_schedule_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jean.schedule.runner'`

- [ ] **Step 3: Write the implementation**

Create `src/jean/schedule/runner.py`:

```python
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from jean.ports import Schedule, ScheduleStore
from jean.schedule.cron import next_after

logger = logging.getLogger("jean.schedule")

Handle = Callable[[str, str, str], Awaitable[None]]


class ScheduleRunner:
    """Fires due schedules as ordinary turns in their own threads.

    Injection happens at SessionManager.handle(), below the gateway: handle()
    already takes the per-thread lock, so cross-worker serialisation is inherited
    rather than rebuilt, and engagement -- which lives in the gateway above it --
    is left untouched. A scheduled post therefore never cuts into a live exchange
    and never makes the agent believe a conversation just started.
    """

    def __init__(
        self,
        store: ScheduleStore,
        handle: Handle,
        *,
        grace_seconds: float = 3600.0,
        poll_seconds: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._handle = handle
        self._grace_seconds = grace_seconds
        self._poll_seconds = poll_seconds
        self._clock = clock

    def _advance(self, schedule: Schedule) -> float:
        return next_after(schedule.cron, schedule.timezone, schedule.next_run_at)

    async def run_once(self) -> int:
        """Claim everything due and fire it. Returns how many actually ran."""
        now = self._clock()
        claimed = await self._store.claim_due_schedules(now, self._advance)
        fired = 0
        for schedule in claimed:
            late = now - schedule.next_run_at
            if late > self._grace_seconds:
                logger.warning(
                    "schedule %s missed by %.0fs (grace %.0fs) -- skipping to next occurrence",
                    schedule.id,
                    late,
                    self._grace_seconds,
                )
                await self._store.record_run(
                    schedule.id, last_run_at=now, last_status="missed"
                )
                continue
            try:
                await self._handle(schedule.channel, schedule.thread_ts, schedule.prompt)
            except Exception:
                # One bad schedule must not stop the rest, nor kill the loop.
                # next_run_at already advanced, so this retries at the next
                # occurrence rather than immediately.
                logger.exception("schedule %s failed", schedule.id)
                await self._store.record_run(
                    schedule.id, last_run_at=now, last_status="error"
                )
                continue
            await self._store.record_run(schedule.id, last_run_at=now, last_status="ok")
            fired += 1
        return fired

    async def run(self) -> None:
        """Background loop: poll, fire what is due, never die."""
        while True:
            await asyncio.sleep(self._poll_seconds)
            try:
                await self.run_once()
            except Exception:
                logger.exception("schedule poll failed")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_schedule_runner.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Verify and commit**

```bash
./scripts/verify.sh
git add src/jean/schedule/runner.py tests/test_schedule_runner.py
git commit -m "feat(schedule): runner with grace window and per-schedule isolation"
```

---

### Task 5: Agent-facing MCP tools

**Files:**
- Create: `src/jean/schedule/mcp.py`
- Test: `tests/test_schedule_mcp.py`

**Interfaces:**
- Consumes: `ScheduleStore` (Task 2), `validate`/`next_after`/`CronError` (Task 1), `ApprovalGate.request(channel, thread_ts, summary) -> ApprovalDecision` with `.approved` and `.by`.
- Produces: `build_schedule_mcp(store, gate, *, channel, thread_ts, clock=time.time) -> tuple[Any, list[str], list[SdkMcpTool]]` — same triple shape as `build_slack_mcp`.

Tools: `create(cron, timezone, prompt)`, `list()`, `remove(id)`. Server key `jean_schedule`, so the tool ids are `mcp__jean_schedule__create`, `mcp__jean_schedule__list`, `mcp__jean_schedule__remove`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_schedule_mcp.py`:

```python
from __future__ import annotations

from typing import Any

from jean.db.memory import MemoryStore
from jean.ports import ApprovalDecision
from jean.schedule.mcp import build_schedule_mcp


class FakeGate:
    def __init__(self, approved: bool = True, by: str = "U9") -> None:
        self.requests: list[tuple[str, str, str]] = []
        self._decision = ApprovalDecision(approved, by)

    async def request(self, channel: str, thread_ts: str, summary: str) -> ApprovalDecision:
        self.requests.append((channel, thread_ts, summary))
        return self._decision


def _tools(store: MemoryStore, gate: FakeGate) -> dict[str, Any]:
    _server, _names, tools = build_schedule_mcp(
        store, gate, channel="C1", thread_ts="111.222", clock=lambda: 1000.0
    )
    return {t.name: t for t in tools}


def _text(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


async def test_create_asks_for_approval_and_writes_on_approve() -> None:
    store, gate = MemoryStore(), FakeGate(approved=True, by="U9")
    result = await _tools(store, gate)["create"].handler(
        {"cron": "0 9 * * 2", "timezone": "Asia/Jakarta", "prompt": "sprint summary"}
    )

    assert len(gate.requests) == 1
    rows = await store.list_schedules("C1", "111.222")
    assert len(rows) == 1
    assert rows[0].cron == "0 9 * * 2"
    # The approver is the accountable party, so that is who owns the row.
    assert rows[0].created_by == "U9"
    assert not result.get("is_error")


async def test_denied_creates_nothing() -> None:
    store, gate = MemoryStore(), FakeGate(approved=False)
    result = await _tools(store, gate)["create"].handler(
        {"cron": "0 9 * * 2", "timezone": "Asia/Jakarta", "prompt": "sprint summary"}
    )

    assert await store.list_schedules("C1", "111.222") == []
    assert result.get("is_error") is True


async def test_invalid_cron_never_reaches_the_approver() -> None:
    # Nobody should be asked to approve a schedule that cannot run.
    store, gate = MemoryStore(), FakeGate()
    result = await _tools(store, gate)["create"].handler(
        {"cron": "not a cron", "timezone": "UTC", "prompt": "p"}
    )

    assert gate.requests == []
    assert await store.list_schedules("C1", "111.222") == []
    assert result.get("is_error") is True


async def test_create_binds_the_calling_thread_not_its_arguments() -> None:
    store, gate = MemoryStore(), FakeGate()
    await _tools(store, gate)["create"].handler(
        {
            "cron": "0 9 * * 2",
            "timezone": "UTC",
            "prompt": "p",
            "channel": "C-EVIL",
            "thread_ts": "666.666",
        }
    )

    assert await store.list_schedules("C-EVIL", "666.666") == []
    assert len(await store.list_schedules("C1", "111.222")) == 1


async def test_list_shows_only_this_thread() -> None:
    store, gate = MemoryStore(), FakeGate()
    await store.create_schedule(
        channel="C1", thread_ts="111.222", cron="0 9 * * 2", timezone="UTC",
        prompt="mine", created_by="U1", next_run_at=1000.0,
    )
    await store.create_schedule(
        channel="C1", thread_ts="999.999", cron="0 9 * * 2", timezone="UTC",
        prompt="theirs", created_by="U1", next_run_at=1000.0,
    )

    text = _text(await _tools(store, gate)["list"].handler({}))

    assert "mine" in text
    assert "theirs" not in text


async def test_remove_asks_for_approval_and_deletes_on_approve() -> None:
    store, gate = MemoryStore(), FakeGate(approved=True)
    row = await store.create_schedule(
        channel="C1", thread_ts="111.222", cron="0 9 * * 2", timezone="UTC",
        prompt="p", created_by="U1", next_run_at=1000.0,
    )

    await _tools(store, gate)["remove"].handler({"id": row.id})

    assert len(gate.requests) == 1
    assert await store.list_schedules("C1", "111.222") == []


async def test_remove_denied_keeps_the_schedule() -> None:
    store, gate = MemoryStore(), FakeGate(approved=False)
    row = await store.create_schedule(
        channel="C1", thread_ts="111.222", cron="0 9 * * 2", timezone="UTC",
        prompt="p", created_by="U1", next_run_at=1000.0,
    )

    result = await _tools(store, gate)["remove"].handler({"id": row.id})

    assert len(await store.list_schedules("C1", "111.222")) == 1
    assert result.get("is_error") is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_schedule_mcp.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jean.schedule.mcp'`

- [ ] **Step 3: Write the implementation**

Create `src/jean/schedule/mcp.py`:

```python
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool

from jean.approval.gate import ApprovalGate
from jean.ports import ScheduleStore
from jean.schedule.cron import CronError, next_after, validate

_CREATE_SCHEMA = {
    "type": "object",
    "properties": {
        "cron": {
            "type": "string",
            "description": "5-field cron, e.g. '0 9 * * 2' for Tuesdays at 09:00",
        },
        "timezone": {
            "type": "string",
            "description": "IANA timezone the cron is read in, e.g. 'Asia/Jakarta'",
        },
        "prompt": {
            "type": "string",
            "description": "what to do each time it fires, phrased as an instruction",
        },
    },
    "required": ["cron", "timezone", "prompt"],
}


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def build_schedule_mcp(
    store: ScheduleStore,
    gate: ApprovalGate,
    *,
    channel: str,
    thread_ts: str,
    clock: Callable[[], float] = time.time,
) -> tuple[Any, list[str], list[SdkMcpTool]]:
    """Build the in-process `jean_schedule` MCP server for ONE Slack thread.

    channel/thread_ts are bound here, at construction, exactly as in
    build_slack_mcp: the SDK invokes these tools lazily, so reading a shared slot
    at call time would let a slow turn on thread A write a schedule into thread B.

    The approval gate is called INSIDE create/remove. An agent cannot write a row
    by choosing not to ask -- that decision lives in code, never in the prompt.
    """

    def _describe(cron: str, timezone: str, when: float) -> str:
        local = datetime.fromtimestamp(when, ZoneInfo(timezone))
        return f"`{cron}` ({timezone}), next run {local:%a %d %b %H:%M}"

    async def _create(args: dict[str, Any]) -> dict[str, Any]:
        cron = str(args.get("cron", ""))
        timezone = str(args.get("timezone", ""))
        prompt = str(args.get("prompt", ""))
        if not prompt.strip():
            return _err("prompt is required")
        # Validate BEFORE asking a human: nobody should be shown Approve/Deny
        # for a schedule that could never run.
        try:
            validate(cron, timezone)
            when = next_after(cron, timezone, clock())
        except CronError as exc:
            return _err(str(exc))

        decision = await gate.request(
            channel,
            thread_ts,
            f"create a schedule: {_describe(cron, timezone, when)} -- {prompt}",
        )
        if not decision.approved:
            return _err("Schedule not created: the request was denied.")

        row = await store.create_schedule(
            channel=channel,
            thread_ts=thread_ts,
            cron=cron,
            timezone=timezone,
            prompt=prompt,
            created_by=decision.by,
            next_run_at=when,
        )
        return _ok(f"Scheduled ({row.id}): {_describe(cron, timezone, when)}")

    async def _list(_args: dict[str, Any]) -> dict[str, Any]:
        rows = await store.list_schedules(channel, thread_ts)
        if not rows:
            return _ok("No schedules in this thread.")
        lines = [
            f"{r.id}: {_describe(r.cron, r.timezone, r.next_run_at)} -- {r.prompt}"
            + (f" [last: {r.last_status}]" if r.last_status else "")
            for r in rows
        ]
        return _ok("\n".join(lines))

    async def _remove(args: dict[str, Any]) -> dict[str, Any]:
        schedule_id = str(args.get("id", ""))
        rows = await store.list_schedules(channel, thread_ts)
        match = next((r for r in rows if r.id == schedule_id), None)
        # Thread-scoped lookup: an id from another thread is simply not found.
        if match is None:
            return _err(f"No schedule {schedule_id!r} in this thread.")

        decision = await gate.request(
            channel,
            thread_ts,
            f"remove the schedule {_describe(match.cron, match.timezone, match.next_run_at)}",
        )
        if not decision.approved:
            return _err("Schedule not removed: the request was denied.")

        await store.delete_schedule(schedule_id, channel, thread_ts)
        return _ok(f"Removed schedule {schedule_id}.")

    tools = [
        tool(
            "create",
            "Create a recurring prompt in this thread. Asks a human to approve first.",
            _CREATE_SCHEMA,
        )(_create),
        tool("list", "List the schedules in this thread.", {})(_list),
        tool(
            "remove",
            "Remove a schedule from this thread. Asks a human to approve first.",
            {"id": str},
        )(_remove),
    ]
    server = create_sdk_mcp_server("jean_schedule", tools=tools)
    names = [f"mcp__jean_schedule__{t.name}" for t in tools]
    return server, names, tools
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_schedule_mcp.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Verify and commit**

```bash
./scripts/verify.sh
git add src/jean/schedule/mcp.py tests/test_schedule_mcp.py
git commit -m "feat(schedule): create/list/remove tools gated by the approval gate"
```

---

### Task 6: Config and wiring

**Files:**
- Modify: `src/jean/config.py` (two settings)
- Modify: `src/jean/server.py` (build the server, register tools, start the loop)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `ScheduleRunner` (Task 4), `build_schedule_mcp` (Task 5).
- Produces: nothing further.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`, matching the file's existing style for constructing `Settings`:

```python
def test_schedule_settings_have_defaults() -> None:
    settings = Settings(slack_bot_token="x", slack_app_token="y")
    assert settings.schedule_poll_seconds == 30.0
    assert settings.schedule_grace_seconds == 3600.0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_config.py -k schedule -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'schedule_poll_seconds'`

- [ ] **Step 3: Add the settings**

In `src/jean/config.py`, beside the other interval settings:

```python
    # How often a worker looks for due schedules, and how late a firing may be
    # and still run. A summary forty minutes late is fine; two days late carries
    # a "weekly" framing that is no longer true, so it is recorded as missed.
    schedule_poll_seconds: float = 30.0
    schedule_grace_seconds: float = 3600.0
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_config.py -k schedule -v`
Expected: PASS

- [ ] **Step 5: Wire it into the composition root**

In `src/jean/server.py`:

1. Import `ScheduleRunner` and `build_schedule_mcp`.
2. Where `build_slack_mcp(...)` is called per session, also call `build_schedule_mcp(store, gate, channel=channel, thread_ts=thread_ts)` and add its server to `mcp_servers` and its names to `allowed_tools`, exactly as the slack server's are added.
3. The `tasks` list is built around line 294 and currently reads:

```python
    tasks = [
        AsyncSocketModeHandler(app, settings.slack_app_token).start_async(),
        manager.run_sweeper(),
    ]
    if settings.cleanup_enabled:
        scheduler = build_cleanup_scheduler(store, settings)
        tasks.append(scheduler.run())
```

Add directly after that `if` block:

```python
    schedule_runner = ScheduleRunner(
        store,
        manager.handle,
        grace_seconds=settings.schedule_grace_seconds,
        poll_seconds=settings.schedule_poll_seconds,
    )
    tasks.append(schedule_runner.run())
```

Name it `schedule_runner`, not `runner`: `runner` is already bound in this
function to the aiohttp web runner and is used in the `finally` block
(`await runner.cleanup()`). Shadowing it would break shutdown.

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest -v`
Expected: PASS, no warnings.

- [ ] **Step 7: Verify and commit**

```bash
./scripts/verify.sh
git add src/jean/config.py src/jean/server.py tests/test_config.py
git commit -m "feat(schedule): wire runner and tools into the composition root"
```

---

## Manual verification

After Task 6, with a Slack workspace and a database:

1. In a thread, ask the agent for something recurring — "post a one-line status every minute" with a cron of `* * * * *` for a fast loop.
2. Confirm Approve/Deny buttons appear **in that thread**.
3. Deny once. Confirm nothing is created (`mcp__jean_schedule__list` shows none).
4. Ask again, approve. Confirm the confirmation names the next run time.
5. Wait for it to fire. Confirm the output is a **reply in that thread**, not a channel post.
6. Confirm a message from a third party in that thread is still ignored — the firing must not have changed engagement.
7. Remove the schedule, approving when asked. Confirm it stops firing.

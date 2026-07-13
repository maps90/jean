# Session Transcript Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist each thread's claude-agent transcript in Postgres so any worker can resume any thread, and expire it after 3 idle days.

**Architecture:** The Claude Code CLI keeps a thread's conversation in a local `.jsonl` whose path is a pure function of config; that file is the *entire* session state (verified — a transcript dropped into a virgin project dir resumes correctly). So each turn: hydrate the file from Postgres on a cold client, run the turn, write the gzipped file back. A `turn_seq` counter on the session row invalidates a worker's cached client when another worker has advanced the thread. Retention deletes the session row at 3 idle days; the transcript cascades away with it.

**Tech Stack:** Python 3.11+, asyncpg, pytest/pytest-asyncio (`asyncio_mode = "auto"`), pydantic-settings, stdlib `gzip`.

**Spec:** `docs/superpowers/specs/2026-07-13-session-transcript-persistence-design.md`

## Global Constraints

- `from __future__ import annotations` at the top of every module; modern hints (`str | None`).
- Domain modules (`session/`, `gateway/`, `approval/`, `persona/`) must NOT import `asyncpg`, `slack_bolt`, `slack_sdk`, or construct `ClaudeSDKClient`. Collaborators arrive by injection. Only `server.py` and the `db/`+`slack/` adapters touch concrete infra.
- TDD: write the failing test, run it, see it fail, then write minimal code.
- No live network and no live Postgres in the default `pytest` run. The asyncpg adapter's tests are skipped unless `JEAN_TEST_DATABASE_URL` is set.
- Test output must be pristine — no stray warnings.
- Before each commit run `./scripts/verify.sh` (ruff check + ruff format-check + pytest).
- Do NOT add AI co-author trailers to commits in this repo.
- Work happens in the worktree `../jean-session-transcripts` on branch `session-transcripts`.
- `MemoryStore` and `PostgresStore` must stay behaviorally identical — every new capability gets a shared assertion in `tests/store_behavior.py` that both run.

## File Structure

| Path | Change | Responsibility |
|---|---|---|
| `src/jean/session/transcript.py` | **create** | `LocalTranscripts` — the one place that knows the CLI's on-disk transcript path; read/write/delete |
| `src/jean/ports.py` | modify | add `TranscriptStore` port; `SessionRow.turn_seq`; `SessionStore.bump_turn`; `MaintenanceStore.prune` takes two cutoffs |
| `src/jean/db/memory.py` | modify | implement `TranscriptStore`, `bump_turn`, two-window prune with cascade |
| `src/jean/db/postgres.py` | modify | `transcripts` table + `turn_seq` column; gzip on save, gunzip on load; two-window prune |
| `src/jean/session/session.py` | modify | hydrate before connect, archive after turn, `turn_seq` stale-client guard, delete local file on close |
| `src/jean/maintenance/cleanup.py` | modify | separate session/approval retention windows; daily interval |
| `src/jean/config.py` | modify | new `JEAN_*` knobs; retire `cleanup_retention_days` |
| `src/jean/server.py` | modify | construct `LocalTranscripts`, inject into `JeanSession`; new scheduler args |
| `tests/test_local_transcripts.py` | **create** | path formula + file round trip |
| `tests/store_behavior.py` | modify | shared assertions both adapters satisfy |
| `tests/test_memory_store.py`, `tests/test_postgres_store.py` | modify | run the new shared assertions |
| `tests/test_session.py` | modify | hydration, archival, stale-client invalidation, archive-failure safety |
| `tests/test_cleanup.py`, `tests/test_config.py` | modify | two windows; new knobs |

---

### Task 1: `LocalTranscripts` — the on-disk transcript locator

The CLI writes each conversation to `$HOME/.claude/projects/<slug>/<sid>.jsonl`, where `slug` is the agent's `cwd` with every `/` and `.` replaced by `-`. This was verified against the installed CLI: `cwd=/Users/d/.jean/workspaces` produces the directory `-Users-d--jean-workspaces` (note the doubled `-` where `/.` collapsed). This class is the only place that knows that formula.

**Files:**
- Create: `src/jean/session/transcript.py`
- Test: `tests/test_local_transcripts.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `LocalTranscripts(cli_home: Path, cwd: Path)` with `path(sdk_session_id: str) -> Path`, `read(sdk_session_id: str) -> bytes | None`, `write(sdk_session_id: str, data: bytes) -> None`, `delete(sdk_session_id: str) -> None`. Used by Task 5 and Task 7.

- [ ] **Step 1: Write the failing test**

Create `tests/test_local_transcripts.py`:

```python
from __future__ import annotations

from pathlib import Path

from jean.session.transcript import LocalTranscripts


def test_path_matches_the_cli_slug_formula(tmp_path: Path):
    # Verified against the real CLI: cwd `/Users/d/.jean/workspaces` lands in
    # `~/.claude/projects/-Users-d--jean-workspaces/`. Both `/` and `.` become `-`.
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/Users/d/.jean/workspaces"))

    assert local.path("abc-123") == (
        tmp_path / ".claude" / "projects" / "-Users-d--jean-workspaces" / "abc-123.jsonl"
    )


def test_write_read_delete_round_trip(tmp_path: Path):
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))

    assert local.read("sid-1") is None  # nothing on disk yet

    local.write("sid-1", b'{"type":"user"}\n')  # creates parent dirs
    assert local.read("sid-1") == b'{"type":"user"}\n'

    local.delete("sid-1")
    assert local.read("sid-1") is None

    local.delete("sid-1")  # deleting a missing transcript is not an error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ../jean-session-transcripts && uv run pytest tests/test_local_transcripts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jean.session.transcript'`

- [ ] **Step 3: Write minimal implementation**

Create `src/jean/session/transcript.py`:

```python
from __future__ import annotations

import re
from pathlib import Path


class LocalTranscripts:
    """The claude CLI's transcript files on *this* pod's disk.

    The CLI writes each conversation to `$HOME/.claude/projects/<slug>/<id>.jsonl`,
    where the slug is its `cwd` with every `/` and `.` turned into `-`. jean gives
    every thread the same `cwd` (settings.home/"workspaces"), so there is exactly one
    project directory and transcripts differ only by session id.

    That file is the whole of a session's state: dropping it into a project dir the
    CLI has never seen and resuming by id replays the conversation. Postgres holds the
    durable copy (TranscriptStore); this class is only the local materialization.
    """

    def __init__(self, cli_home: Path, cwd: Path) -> None:
        self._dir = cli_home / ".claude" / "projects" / self._slug(cwd)

    @staticmethod
    def _slug(cwd: Path) -> str:
        return re.sub(r"[/.]", "-", str(cwd))

    def path(self, sdk_session_id: str) -> Path:
        return self._dir / f"{sdk_session_id}.jsonl"

    def read(self, sdk_session_id: str) -> bytes | None:
        path = self.path(sdk_session_id)
        if not path.exists():
            return None
        return path.read_bytes()

    def write(self, sdk_session_id: str, data: bytes) -> None:
        path = self.path(sdk_session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def delete(self, sdk_session_id: str) -> None:
        self.path(sdk_session_id).unlink(missing_ok=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_local_transcripts.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
./scripts/verify.sh
git add src/jean/session/transcript.py tests/test_local_transcripts.py
git commit -m "feat(session): locate the CLI's on-disk transcript for a session id"
```

---

### Task 2: Ports — `TranscriptStore`, `turn_seq`, two-window prune

Port changes only, plus the `MemoryStore` implementation, so the domain has something to depend on. `PostgresStore` catches up in Task 3 — expect its tests to fail between these two tasks only if `JEAN_TEST_DATABASE_URL` is set; the default run stays green because `prune`'s signature change is applied to both adapters in this task (Postgres gets the *transcript* methods in Task 3).

**Files:**
- Modify: `src/jean/ports.py`
- Modify: `src/jean/db/memory.py`
- Modify: `src/jean/db/postgres.py:211` (prune signature only)
- Modify: `src/jean/maintenance/cleanup.py` (call site of `prune`)
- Modify: `tests/store_behavior.py`, `tests/test_memory_store.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `SessionRow.turn_seq: int` (defaults to `0`)
  - `SessionStore.bump_turn(channel: str, thread_ts: str) -> int` — increments and returns the new `turn_seq`; creates the row if absent.
  - `TranscriptStore.save(channel, thread_ts, sdk_session_id: str, data: bytes) -> None` and `TranscriptStore.load(channel, thread_ts, sdk_session_id: str) -> bytes | None`. `load` returns `None` when nothing is stored **or** when the stored transcript belongs to a different session id.
  - `MaintenanceStore.prune(*, sessions_older_than: float, approvals_older_than: float) -> PruneResult`
  - Deleting a session removes its transcript.

- [ ] **Step 1: Write the failing shared assertions**

Append to `tests/store_behavior.py`:

```python
async def assert_turn_seq_increments(store) -> None:
    channel, thread_ts = "C-seq", "900.1"
    await store.upsert_session(channel, thread_ts, sdk_session_id="sdk-1")
    assert (await store.get_session(channel, thread_ts)).turn_seq == 0

    assert await store.bump_turn(channel, thread_ts) == 1
    assert await store.bump_turn(channel, thread_ts) == 2
    assert (await store.get_session(channel, thread_ts)).turn_seq == 2


async def assert_transcript_roundtrip(store) -> None:
    channel, thread_ts = "C-tr", "901.1"
    await store.upsert_session(channel, thread_ts, sdk_session_id="sid-a")

    assert await store.load(channel, thread_ts, "sid-a") is None

    blob = b'{"type":"user","sessionId":"sid-a"}\n' * 50
    await store.save(channel, thread_ts, "sid-a", blob)
    assert await store.load(channel, thread_ts, "sid-a") == blob

    # A transcript stored under a different session id must never be handed back:
    # resuming with the wrong transcript would corrupt the thread's memory.
    assert await store.load(channel, thread_ts, "sid-b") is None

    # The newest turn's transcript replaces the previous one.
    bigger = blob + b'{"type":"assistant"}\n'
    await store.save(channel, thread_ts, "sid-a", bigger)
    assert await store.load(channel, thread_ts, "sid-a") == bigger


async def assert_prune_uses_separate_windows_and_drops_transcripts(store) -> None:
    now = time.time()
    old, recent = now - 10 * 86400, now - 1 * 86400

    # A session idle 10 days, with a transcript.
    await store.upsert_session("C-old", "1.0", sdk_session_id="sid-old")
    await store.save("C-old", "1.0", "sid-old", b"stale bytes")
    # A session idle 1 day.
    await store.upsert_session("C-new", "2.0", sdk_session_id="sid-new")

    await _backdate_session(store, "C-old", "1.0", old)
    await _backdate_session(store, "C-new", "2.0", recent)

    # Sessions expire at 3 days; approvals at 30. The 10-day session goes; the
    # 1-day one stays. A single shared window could not express this.
    result = await store.prune(
        sessions_older_than=now - 3 * 86400,
        approvals_older_than=now - 30 * 86400,
    )

    assert result.sessions_deleted == 1
    assert await store.get_session("C-old", "1.0") is None
    assert await store.get_session("C-new", "2.0") is not None
    # the transcript went with its session row
    assert await store.load("C-old", "1.0", "sid-old") is None
```

Add `import time` to the imports at the top of `tests/store_behavior.py` (it currently imports only `asyncio`), and add this helper next to the other functions:

```python
async def _backdate_session(store, channel: str, thread_ts: str, when: float) -> None:
    """Force a session's last_active_at into the past. Both adapters store it as
    an epoch float, but only through their own writes -- so reach in per adapter."""
    if hasattr(store, "_sessions"):  # MemoryStore
        store._sessions[(channel, thread_ts)].last_active_at = when
    else:  # PostgresStore
        await store._pool.execute(
            "UPDATE sessions SET last_active_at=$3 WHERE channel=$1 AND thread_ts=$2",
            channel,
            thread_ts,
            when,
        )
```

Wire them into `tests/test_memory_store.py` — follow the file's existing pattern of one test per shared assertion:

```python
async def test_turn_seq_increments():
    await assert_turn_seq_increments(MemoryStore())


async def test_transcript_roundtrip():
    await assert_transcript_roundtrip(MemoryStore())


async def test_prune_uses_separate_windows_and_drops_transcripts():
    await assert_prune_uses_separate_windows_and_drops_transcripts(MemoryStore())
```

(import the three new names from `tests.store_behavior` alongside the existing ones)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_memory_store.py -v`
Expected: FAIL — `AttributeError: 'MemoryStore' object has no attribute 'bump_turn'`

- [ ] **Step 3: Write minimal implementation**

In `src/jean/ports.py`, add `turn_seq` to `SessionRow`:

```python
@dataclass
class SessionRow:
    channel: str
    thread_ts: str
    sdk_session_id: str | None
    permission_mode: str | None
    engaged: bool
    last_active_at: float
    turn_seq: int = 0
```

Add `bump_turn` to the `SessionStore` protocol (after `is_engaged`):

```python
    async def bump_turn(self, channel: str, thread_ts: str) -> int: ...
```

Add the new port after `SessionStore`:

```python
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
    async def load(
        self, channel: str, thread_ts: str, sdk_session_id: str
    ) -> bytes | None: ...
```

Change `MaintenanceStore.prune` to take two cutoffs:

```python
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
```

In `src/jean/db/memory.py`, add transcript storage to `__init__`:

```python
        self._transcripts: dict[tuple[str, str], tuple[str, bytes]] = {}
```

Add `turn_seq` to both `SessionRow` constructions in `get_session` and `upsert_session`. In `get_session`, pass `turn_seq=row.turn_seq`. In `upsert_session`, preserve it: `turn_seq=existing.turn_seq if existing else 0`.

Add after `is_engaged`:

```python
    async def bump_turn(self, channel: str, thread_ts: str) -> int:
        key = (channel, thread_ts)
        if key not in self._sessions:
            await self.upsert_session(channel, thread_ts, touch=False)
        row = self._sessions[key]
        row.turn_seq += 1
        return row.turn_seq

    # ---- TranscriptStore ----
    async def save(
        self, channel: str, thread_ts: str, sdk_session_id: str, data: bytes
    ) -> None:
        self._transcripts[(channel, thread_ts)] = (sdk_session_id, data)

    async def load(
        self, channel: str, thread_ts: str, sdk_session_id: str
    ) -> bytes | None:
        stored = self._transcripts.get((channel, thread_ts))
        if stored is None or stored[0] != sdk_session_id:
            return None
        return stored[1]
```

Replace `MemoryStore.prune`:

```python
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
```

In `src/jean/db/postgres.py`, change only the `prune` signature and its two comparisons (the transcript table arrives in Task 3):

```python
    async def prune(
        self, *, sessions_older_than: float, approvals_older_than: float
    ) -> PruneResult:
        async with self._pool.acquire() as c, c.transaction():
            appr = await c.execute(
                "DELETE FROM approvals WHERE resolved_at IS NOT NULL AND resolved_at < $1",
                approvals_older_than,
            )
            sess = await c.execute(
                "DELETE FROM sessions WHERE last_active_at < $1", sessions_older_than
            )
        return PruneResult(
            approvals_deleted=int(appr.split()[-1]),
            sessions_deleted=int(sess.split()[-1]),
        )
```

In `src/jean/maintenance/cleanup.py`, update the single call site inside `run_once` so the module still imports and the existing tests keep running (Task 6 gives it real separate windows):

```python
        cutoff = self._clock() - self._retention_seconds
        result = await self._store.prune(
            sessions_older_than=cutoff, approvals_older_than=cutoff
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_memory_store.py tests/test_cleanup.py tests/test_ports.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
./scripts/verify.sh
git add src/jean/ports.py src/jean/db/memory.py src/jean/db/postgres.py src/jean/maintenance/cleanup.py tests/store_behavior.py tests/test_memory_store.py
git commit -m "feat(ports): add TranscriptStore, turn_seq, and separate prune windows"
```

---

### Task 3: Postgres adapter — `transcripts` table, gzip, `turn_seq`

**Files:**
- Modify: `src/jean/db/postgres.py`
- Modify: `tests/test_postgres_store.py`

**Interfaces:**
- Consumes: the ports from Task 2.
- Produces: `PostgresStore` satisfying `TranscriptStore` and the new `SessionStore.bump_turn`. Compression is internal — the port's bytes go in and come back identical.

- [ ] **Step 1: Write the failing test**

In `tests/test_postgres_store.py`, add tests running the three new shared assertions. Follow the file's existing skip-unless-`JEAN_TEST_DATABASE_URL` pattern and its existing fixture; import `assert_turn_seq_increments`, `assert_transcript_roundtrip`, and `assert_prune_uses_separate_windows_and_drops_transcripts` from `tests.store_behavior`:

```python
async def test_turn_seq_increments(store):
    await assert_turn_seq_increments(store)


async def test_transcript_roundtrip(store):
    await assert_transcript_roundtrip(store)


async def test_prune_uses_separate_windows_and_drops_transcripts(store):
    await assert_prune_uses_separate_windows_and_drops_transcripts(store)


async def test_transcript_is_compressed_at_rest(store):
    """The blob is gzipped in the column -- that's what makes the ~4.4x saving
    real -- but that is the adapter's business, invisible through the port."""
    await store.upsert_session("C-gz", "1.0", sdk_session_id="sid-gz")
    blob = b'{"type":"user","text":"hello hello hello"}\n' * 200
    await store.save("C-gz", "1.0", "sid-gz", blob)

    stored = await store._pool.fetchval(
        "SELECT data FROM transcripts WHERE channel='C-gz' AND thread_ts='1.0'"
    )
    assert len(stored) < len(blob)  # compressed on disk
    assert await store.load("C-gz", "1.0", "sid-gz") == blob  # identical through the port

    raw = await store._pool.fetchval(
        "SELECT raw_bytes FROM transcripts WHERE channel='C-gz' AND thread_ts='1.0'"
    )
    assert raw == len(blob)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `JEAN_TEST_DATABASE_URL=postgresql://localhost/jean_test uv run pytest tests/test_postgres_store.py -v`
Expected: FAIL — `asyncpg.exceptions.UndefinedTableError: relation "transcripts" does not exist` (or `AttributeError: bump_turn` first).
If you have no local Postgres, these are skipped; CI runs them against its Postgres service. Do not require a live DB for the default run.

- [ ] **Step 3: Write minimal implementation**

In `src/jean/db/postgres.py`, add `import gzip` at the top. Extend `_SCHEMA`:

```python
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
```

(`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` and `ALTER COLUMN ... SET STORAGE` are both idempotent, so the boot-time schema apply stays safe to re-run. `STORAGE EXTERNAL` tells Postgres not to attempt its own pglz pass over bytes we already gzipped.)

Add `turn_seq=r["turn_seq"]` to the `SessionRow` built in `get_session`.

Add after `is_engaged`:

```python
    async def bump_turn(self, channel: str, thread_ts: str) -> int:
        # INSERT..ON CONFLICT so a bump on a thread with no row yet still works;
        # RETURNING hands back the new value the caller must remember.
        return await self._pool.fetchval(
            """INSERT INTO sessions(channel,thread_ts,turn_seq) VALUES($1,$2,1)
               ON CONFLICT(channel,thread_ts) DO UPDATE SET turn_seq=sessions.turn_seq+1
               RETURNING turn_seq""",
            channel,
            thread_ts,
        )

    # ---- TranscriptStore ----  gzip is a storage detail; the port speaks raw bytes.
    async def save(
        self, channel: str, thread_ts: str, sdk_session_id: str, data: bytes
    ) -> None:
        await self._pool.execute(
            """INSERT INTO transcripts(channel,thread_ts,sdk_session_id,data,raw_bytes,updated_at)
               VALUES($1,$2,$3,$4,$5,extract(epoch from now()))
               ON CONFLICT(channel,thread_ts) DO UPDATE SET
                 sdk_session_id=$3, data=$4, raw_bytes=$5,
                 updated_at=extract(epoch from now())""",
            channel,
            thread_ts,
            sdk_session_id,
            gzip.compress(data),
            len(data),
        )

    async def load(
        self, channel: str, thread_ts: str, sdk_session_id: str
    ) -> bytes | None:
        r = await self._pool.fetchrow(
            "SELECT sdk_session_id, data FROM transcripts WHERE channel=$1 AND thread_ts=$2",
            channel,
            thread_ts,
        )
        if r is None or r["sdk_session_id"] != sdk_session_id:
            return None
        return gzip.decompress(r["data"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `JEAN_TEST_DATABASE_URL=... uv run pytest tests/test_postgres_store.py -v`
Expected: PASS (or SKIPPED with no DB — then rely on CI)
Also run: `uv run pytest -q` → the default suite must stay green.

- [ ] **Step 5: Commit**

```bash
./scripts/verify.sh
git add src/jean/db/postgres.py tests/test_postgres_store.py
git commit -m "feat(db): store thread transcripts as gzipped blobs, cascading with the session"
```

---

### Task 4: `JeanSession` — hydrate, archive, and the `turn_seq` staleness guard

This is the heart of the feature and it carries the bug fix. `JeanSession` caches its client across turns and a cached client **skips `_connect()` entirely** (`session/session.py:103`), so it never re-reads the store. Under N>1 that means: pod A caches a client; pod B serves the next turn and advances the transcript; the turn after that returns to pod A, whose client never saw pod B's turn — it answers from a divergent history and archives a stale blob over the good one. `turn_seq` is what detects that.

**Files:**
- Modify: `src/jean/session/session.py`
- Modify: `tests/test_session.py`

**Interfaces:**
- Consumes: `LocalTranscripts` (Task 1); `TranscriptStore`, `SessionStore.bump_turn`, `SessionRow.turn_seq` (Task 2/3).
- Produces: `JeanSession(channel, thread_ts, *, store, chat, routing, options_factory, client_factory, transcripts, local, max_transcript_bytes=32*1024*1024)`. Used by `server.py` in Task 7.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_session.py`. Note `FakeSdkClient.receive_response` yields `session_id="sdk-session-abc"`, so that is the id the CLI would have written a transcript under; the fake CLI has no real child process, so tests write the "CLI's" file themselves via `local`.

```python
from pathlib import Path

from jean.session.transcript import LocalTranscripts


def _session(store, chat, factory, local, **kw):
    return JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        routing=RoutingContext(),
        options_factory=lambda resume: {"resume": resume},
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
    assert calls[0]["options"] == {"resume": "sdk-session-abc"}
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
    assert calls_a[1]["options"] == {"resume": "sdk-session-abc"}


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
        routing=RoutingContext(),
        options_factory=lambda resume: {"resume": resume},
        client_factory=factory,
        transcripts=BrokenTranscripts(),
        local=local,
    )

    class WritingClient(FakeSdkClient):
        async def query(self, text: str) -> None:
            await super().query(text)
            local.write("sdk-session-abc", b'{"type":"user"}\n')

    session._client_factory = lambda *, options: WritingClient(options=options)

    await session.run_turn("hello")  # the turn still succeeds; the user got an answer
    await session.close()

    assert local.read("sdk-session-abc") == b'{"type":"user"}\n'  # not deleted
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_session.py -v`
Expected: FAIL — `TypeError: JeanSession.__init__() got an unexpected keyword argument 'transcripts'`

- [ ] **Step 3: Write minimal implementation**

Rewrite `src/jean/session/session.py`. Add `import logging` and a module logger; add the imports `from jean.ports import ChatSurface, SessionStore, TranscriptStore` and `from jean.session.transcript import LocalTranscripts`.

`__init__` gains three parameters and three pieces of state:

```python
    def __init__(
        self,
        channel: str,
        thread_ts: str,
        *,
        store: SessionStore,
        chat: ChatSurface,
        routing: RoutingContext,
        options_factory: Callable[[str | None], Any],
        client_factory: Callable[..., Any],
        transcripts: TranscriptStore,
        local: LocalTranscripts,
        max_transcript_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        ...
        self._transcripts = transcripts
        self._local = local
        self._max_transcript_bytes = max_transcript_bytes
        self._client: Any | None = None
        self._seen_seq: int = 0        # turn_seq this instance's client is current with
        self._sid: str | None = None   # session id its transcript is stored under
        self._archived = False         # is the store's copy up to date with local disk?
```

In `_connect`, hydrate before opening (insert after `resume` is read from the row):

```python
        row = await self._store.get_session(self._channel, self._thread_ts)
        resume = row.sdk_session_id if row else None
        self._seen_seq = row.turn_seq if row else 0
        if resume is None:
            return await self._open(None)

        # This worker may never have seen this thread: the CLI resumes from a local
        # file, so materialize Postgres's copy onto our disk first. Without this,
        # `resume` finds nothing and the turn silently starts fresh.
        data = await self._transcripts.load(self._channel, self._thread_ts, resume)
        if data is not None:
            self._local.write(resume, data)
        try:
            return await self._open(resume)
        except Exception:
            client = await self._open(None)
        ...
```

Add archival, and the staleness guard at the top of `run_turn`:

```python
    async def _archive(self, sdk_session_id: str) -> None:
        """Copy this pod's transcript into the store so any worker can resume it."""
        data = self._local.read(sdk_session_id)
        if data is None:
            return
        if len(data) > self._max_transcript_bytes:
            logger.warning(
                "transcript for %s/%s is %d bytes (> max %d); not archiving -- this "
                "thread will not resume on another worker",
                self._channel,
                self._thread_ts,
                len(data),
                self._max_transcript_bytes,
            )
            return
        try:
            await self._transcripts.save(self._channel, self._thread_ts, sdk_session_id, data)
            self._sid, self._archived = sdk_session_id, True
        except Exception:
            # The turn already succeeded and the user has their answer; failing it
            # now would help no one. Log loudly and keep the local file as the only
            # copy (close() will not delete an unarchived transcript).
            self._archived = False
            logger.exception(
                "failed to archive transcript for %s/%s", self._channel, self._thread_ts
            )

    async def run_turn(self, text: str) -> None:
        self._routing.channel = self._channel
        self._routing.thread_ts = self._thread_ts
        await self._chat.set_status(self._channel, self._thread_ts, "is thinking...")
        try:
            if self._client is not None:
                # Another worker may have taken a turn on this thread since we cached
                # this client. A cached client never re-reads the store, so it would
                # answer from a history missing that turn and archive over it. The
                # stored turn_seq is how we notice.
                row = await self._store.get_session(self._channel, self._thread_ts)
                if row is None or row.turn_seq != self._seen_seq:
                    await self.close()

            if self._client is None:
                self._client = await self._connect()

            sid: str | None = None
            await self._client.query(text)
            async for msg in self._client.receive_response():
                got = getattr(msg, "session_id", None)
                if got:
                    sid = got
                    await self._store.upsert_session(
                        self._channel, self._thread_ts, sdk_session_id=sid
                    )
            if sid is not None:
                await self._archive(sid)
                self._seen_seq = await self._store.bump_turn(self._channel, self._thread_ts)
        except BaseException:
            ...unchanged teardown...
            raise
        finally:
            await self._chat.set_status(self._channel, self._thread_ts, "")
```

And `close` drops the local file once the store holds it:

```python
    async def close(self) -> None:
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None
        # Postgres is the durable copy, so a pod need not hoard transcripts for
        # threads it is no longer serving -- but never delete one the store failed
        # to take.
        if self._archived and self._sid is not None:
            self._local.delete(self._sid)
            self._archived = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_session.py -v`
Expected: PASS — all pre-existing tests in the file too (they construct `JeanSession` without the new kwargs, so update those constructions to pass `transcripts=store` and `local=LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))`, adding `tmp_path` to their signatures).

- [ ] **Step 5: Commit**

```bash
./scripts/verify.sh
git add src/jean/session/session.py tests/test_session.py
git commit -m "feat(session): hydrate and archive transcripts; drop a client another worker advanced"
```

---

### Task 5: Retention — separate windows, daily interval

**Files:**
- Modify: `src/jean/maintenance/cleanup.py`
- Modify: `src/jean/config.py`
- Modify: `tests/test_cleanup.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: `MaintenanceStore.prune(*, sessions_older_than, approvals_older_than)` (Task 2).
- Produces: `CleanupScheduler(store, *, session_retention_seconds: float, approval_retention_seconds: float, interval_seconds: float = 86400, check_seconds: float = 3600, clock=time.time)`. Settings: `session_retention_days=3`, `approval_retention_days=30`, `cleanup_interval_hours=24`, `transcript_max_mb=32`; `cleanup_retention_days` is removed.

- [ ] **Step 1: Write the failing test**

In `tests/test_cleanup.py`, replace the `retention_seconds=` constructions with the two new kwargs and add:

```python
async def test_sessions_and_approvals_expire_on_separate_schedules():
    """A thread's memory going stale (3 days) and an audit record aging out
    (30 days) are different concerns -- one window cannot express both."""
    seen: list[dict] = []

    class RecordingStore:
        async def try_claim_cleanup(self, min_interval: float) -> bool:
            return True

        async def prune(self, *, sessions_older_than: float, approvals_older_than: float):
            seen.append(
                {"sessions": sessions_older_than, "approvals": approvals_older_than}
            )
            return PruneResult(approvals_deleted=0, sessions_deleted=0)

    scheduler = CleanupScheduler(
        RecordingStore(),
        session_retention_seconds=3 * 86400,
        approval_retention_seconds=30 * 86400,
        clock=lambda: 1_000_000.0,
    )
    await scheduler.run_once()

    assert seen == [
        {"sessions": 1_000_000.0 - 3 * 86400, "approvals": 1_000_000.0 - 30 * 86400}
    ]


def test_cleanup_interval_defaults_to_daily():
    """A 3-day retention window swept weekly would let rows live ~10 days."""
    scheduler = CleanupScheduler(
        object(), session_retention_seconds=1, approval_retention_seconds=1
    )
    assert scheduler._interval_seconds == 86400
```

In `tests/test_config.py`, add:

```python
def test_retention_defaults(monkeypatch):
    monkeypatch.setenv("JEAN_SLACK_BOT_TOKEN", "xoxb-1")
    monkeypatch.setenv("JEAN_SLACK_APP_TOKEN", "xapp-1")
    settings = Settings()
    assert settings.session_retention_days == 3
    assert settings.approval_retention_days == 30
    assert settings.cleanup_interval_hours == 24
    assert settings.transcript_max_mb == 32
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cleanup.py tests/test_config.py -v`
Expected: FAIL — `TypeError: CleanupScheduler.__init__() got an unexpected keyword argument 'session_retention_seconds'`

- [ ] **Step 3: Write minimal implementation**

In `src/jean/maintenance/cleanup.py`, replace `_WEEK_SECONDS` with `_DAY_SECONDS = 86400` and rewrite the constructor and `run_once`:

```python
    def __init__(
        self,
        store: MaintenanceStore,
        *,
        session_retention_seconds: float,
        approval_retention_seconds: float,
        interval_seconds: float = _DAY_SECONDS,
        check_seconds: float = 3600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._session_retention_seconds = session_retention_seconds
        self._approval_retention_seconds = approval_retention_seconds
        self._interval_seconds = interval_seconds
        self._check_seconds = check_seconds
        self._clock = clock

    async def run_once(self) -> PruneResult | None:
        """Claim the cycle and prune if we won; return None if a peer owns it."""
        if not await self._store.try_claim_cleanup(self._interval_seconds):
            return None
        now = self._clock()
        result = await self._store.prune(
            sessions_older_than=now - self._session_retention_seconds,
            approvals_older_than=now - self._approval_retention_seconds,
        )
        logger.info(
            "retention cleanup pruned %d approvals (>%.0fs) and %d sessions with their "
            "transcripts (>%.0fs)",
            result.approvals_deleted,
            self._approval_retention_seconds,
            result.sessions_deleted,
            self._session_retention_seconds,
        )
        return result
```

Update the class docstring's "Each run deletes rows older than `retention_seconds`" to say sessions and approvals expire on separate windows.

In `src/jean/config.py`, replace the cleanup block:

```python
    # Postgres retention cleanup, swept daily by whichever worker claims the cycle.
    # Sessions and approvals expire on separate schedules: a thread's memory going
    # stale is not the same event as an audit record aging out. Deleting a session
    # row also drops its transcript (FK cascade) -- and its engaged/permission_mode,
    # so a thread quiet this long needs a fresh mention to re-engage jean.
    cleanup_enabled: bool = True
    session_retention_days: int = 3
    approval_retention_days: int = 30
    cleanup_interval_hours: int = 24
    # Refuse to archive a pathological transcript rather than let one thread bloat
    # the database. Such a thread keeps working, but only on the worker holding it.
    transcript_max_mb: int = 32
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cleanup.py tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
./scripts/verify.sh
git add src/jean/maintenance/cleanup.py src/jean/config.py tests/test_cleanup.py tests/test_config.py
git commit -m "feat(maintenance): expire sessions at 3 days and approvals at 30, swept daily"
```

---

### Task 6: Wire it up in the composition root

**Files:**
- Modify: `src/jean/server.py:134-164`
- Modify: `tests/test_server_import.py` (only if it asserts on the wiring)

**Interfaces:**
- Consumes: everything above.
- Produces: a running server. `PostgresStore` satisfies `SessionStore`, `TranscriptStore`, `ApprovalCoordinator`, `ThreadLock`, and `MaintenanceStore` structurally, so it is passed as all of them.

- [ ] **Step 1: Write the implementation** (wiring — the behavior it wires is already covered by Tasks 1–5; `tests/test_server_import.py` guards that the module still imports and constructs)

In `src/jean/server.py`, add the imports:

```python
from pathlib import Path

from jean.session.transcript import LocalTranscripts
```

Build the locator next to the other paths (near `settings.home.mkdir(...)`, around line 45). `Path.home()` resolves `$HOME` — the same variable the CLI child inherits, so both agree on where transcripts live:

```python
    transcript_cwd = settings.home / "workspaces"
    local_transcripts = LocalTranscripts(cli_home=Path.home(), cwd=transcript_cwd)
```

Inject into the session factory:

```python
    def session_factory(channel: str, thread_ts: str) -> JeanSession:
        return JeanSession(
            channel,
            thread_ts,
            store=store,
            chat=chat,
            routing=routing,
            options_factory=options_factory,
            client_factory=ClaudeSDKClient,
            transcripts=store,
            local=local_transcripts,
            max_transcript_bytes=settings.transcript_max_mb * 1024 * 1024,
        )
```

And update the scheduler:

```python
    if settings.cleanup_enabled:
        scheduler = CleanupScheduler(
            store,
            session_retention_seconds=settings.session_retention_days * 86400,
            approval_retention_seconds=settings.approval_retention_days * 86400,
            interval_seconds=settings.cleanup_interval_hours * 3600,
        )
        tasks.append(scheduler.run())
```

- [ ] **Step 2: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS, no warnings.

- [ ] **Step 3: Verify the layering rule held**

Run: `grep -rn "asyncpg\|slack_bolt\|slack_sdk" src/jean/session/`
Expected: no output. The domain must not have grown an infra import.

- [ ] **Step 4: Commit**

```bash
./scripts/verify.sh
git add src/jean/server.py
git commit -m "feat(server): wire transcript persistence and the new retention windows"
```

---

### Task 7: Document the model

**Files:**
- Modify: `ARCHITECTURE.md` (module map + the Postgres box)
- Modify: `CLAUDE.md` (the stateless-workers section)

The scaling section of `CLAUDE.md` currently states that a resumed id names a transcript "on local disk" and that `JeanSession` therefore falls back to a fresh session. That is now only the *last-resort* path — the transcript travels through Postgres. Leaving it as-is would send the next contributor down a road we deliberately closed.

- [ ] **Step 1: Update `CLAUDE.md`**

In the "Sessions are resumable" bullet, replace the local-disk caveat with: Postgres holds the session id *and* the transcript the CLI names with it (`transcripts`, gzipped, cascading with the session row). A cold worker materializes the transcript to its local disk before resuming — the CLI resumes from a file, and a `.jsonl` dropped into a project dir it has never seen replays the conversation. The fresh-session fallback stays as the last resort for a transcript that is genuinely gone (e.g. expired by retention), and the "if connecting without `resume` also fails, it was never the resume" rule is unchanged. Add: a cached client is dropped when the stored `turn_seq` moved without it, because a cached client never re-reads the store and would otherwise answer from a history missing another worker's turn.

- [ ] **Step 2: Update `ARCHITECTURE.md`**

Add `session/transcript.py` to the module map ("locate/read/write the CLI's on-disk transcript for a session id") and add transcripts to the Postgres box in the diagram (`sessions, transcripts, approvals`).

- [ ] **Step 3: Commit**

```bash
./scripts/verify.sh
git add CLAUDE.md ARCHITECTURE.md
git commit -m "docs: transcripts live in Postgres, not just the pod that wrote them"
```

---

## Self-Review

**Spec coverage:** schema + `STORAGE EXTERNAL` + cascade (Task 3); separate `transcripts` table off the hot path (Task 3); `TranscriptStore` port with raw bytes, gzip in the adapter (Tasks 2–3); `LocalTranscripts` path formula (Task 1); hydrate-on-cold-connect / archive-per-turn (Task 4); `turn_seq` staleness guard (Tasks 2–4); local disk bounded via `close()` (Task 4 — `SessionManager.sweep` already calls `close()`, so no change is needed there, which is why the spec's "SessionManager evicts" behavior has no task of its own); two retention windows + daily interval + `transcript_max_mb` (Task 5); `Deployment`/`emptyDir` unchanged (no infra task needed — that is the *absence* of a PVC); docs (Task 7). No gaps.

**Placeholders:** none — every code step carries its code.

**Type consistency:** `save`/`load` take `(channel, thread_ts, sdk_session_id, ...)` in ports, both adapters, and both call sites. `bump_turn` returns `int` and is assigned to `_seen_seq` (`int`). `prune` is keyword-only `sessions_older_than` / `approvals_older_than` in the port, both adapters, `CleanupScheduler`, and the tests. `LocalTranscripts` methods are keyed by `sdk_session_id` everywhere.

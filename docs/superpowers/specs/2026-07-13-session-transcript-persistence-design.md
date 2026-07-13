# Session transcript persistence + retention

**Status:** approved · **Date:** 2026-07-13 · **Branch:** `session-transcripts`

## Problem

jean stores each thread's `sdk_session_id` in Postgres, but the Claude Code CLI stores
the *transcript that id names* on the pod's **local disk**
(`$HOME/.claude/projects/<slug(cwd)>/<sid>.jsonl`). Postgres holds the id; the pod holds
the memory.

Under a single replica this only bites on restart. Under N replicas it is the common
path: jean speaks to Slack over **Socket Mode**, where every worker opens its own
WebSocket and Slack picks one arbitrarily per event. There is no thread affinity to
configure — it is not an HTTP load balancer. So thread X's transcript sits on pod A's
disk while roughly `(N-1)/N` of its follow-up messages land on pods B and C, which
resume an id whose file they cannot see. The CLI exits 1 at startup and `JeanSession`
falls back to a fresh session (`session/session.py:88`), posting *"I couldn't pick up
where we left off."*

The session id already sticks. The **memory** does not.

## Findings that shaped the design

All verified against the installed CLI, not assumed:

1. **`--resume` keeps the same session id and appends to the same file.** Turn 1 created
   `d2cc9d03….jsonl` (26,370 B); resuming returned *the same* `session_id` and grew *the
   same* file to 30,743 B. The id is therefore a stable durable key for the life of the
   thread, and the file is append-only.
2. **The `.jsonl` is the entire session state.** A transcript copied into a project dir
   the CLI had never seen, resumed by id, correctly recalled a fact from the earlier
   turn. A cold pod that materializes that one file resumes perfectly. This is the
   assumption the whole design rests on, so it was tested first.
3. **The transcript path is a pure function of config.** `cwd` is the same for every
   thread (`settings.home / "workspaces"`, `agent_options.py:64`), so there is one
   project dir and files differ only by session id. Slug = `cwd` with `/` and `.` → `-`.
4. **Real sizes** (8 live jean threads): mean **144 KB raw → 33 KB gzipped**; heaviest
   427 KB → 85 KB. Compression is **4.4×**, not the 10–20× one might assume — the JSON is
   dense and not very repetitive.

## Options considered

- **Shared RWX volume** — mount one `ReadWriteMany` PVC at the CLI's projects dir on every
  pod; zero application code. The cluster (`aks-okadoc-admin-uaen`) does offer RWX via
  `azurefile-csi`, but Azure Files is **SMB**, and jean deliberately keeps the `claude`
  CLI child alive between turns (the per-worker cache, `idle_minutes`, default 15), so it
  holds the transcript **open** while idle. Pod A holding an open handle while pod B
  appends is harmless on POSIX/NFS but runs into SMB share-mode semantics. Rejected: it
  adds a second stateful system and stakes correctness on an unverified filesystem
  behavior, to move ~33 KB per turn.
- **StatefulSet + per-pod PVC** — fixes restarts, but not routing. Durable per-pod disk is
  only useful if messages return to the same pod, and Socket Mode will not do that.
  Rejected: solves half the problem and locks in stateful pods for it.
- **Slack replay / Slack AI summary** — Slack exposes **no API for its AI thread
  summaries**; they are a client UI feature. The programmatic surface is
  `conversations.replies` (raw messages). Re-priming a fresh session on every cold start
  costs real LLM tokens on ~2/3 of messages under 3 replicas, and is lossy: it recovers
  what was *said*, never what the agent *saw* (files read, `kubectl` output, MCP results).
  Rejected: strictly more expensive than storage and materially worse.
- **Object storage (S3/GCS/Blob)** — a second credential set and a retention path that can
  drift out of sync with the DB, to store megabytes. Rejected as disproportionate.

**Chosen: transcripts in Postgres, pods stay a plain `Deployment`.** Each pod touches only
its own local disk, so no shared-file semantics exist to get wrong. Postgres is already
load-bearing for correctness (advisory locks, approvals, sessions), so this adds no new
stateful dependency. Cost at 50 threads/day with a 3-day window: **~5 MB, well under a
cent per month** (Azure bills PG storage ≈ $0.10/GiB-month).

## Design

### Schema

The blob lives in **its own table**, not on the `sessions` row: `get_session()` does
`SELECT *` and runs on every engagement check, so a bytea on that row would drag the
transcript into every hot query. Separating them leaves the hot path exactly as cheap as
it is today.

```sql
CREATE TABLE IF NOT EXISTS transcripts (
  channel text NOT NULL, thread_ts text NOT NULL,
  sdk_session_id text NOT NULL,
  data bytea NOT NULL,              -- gzip(<sid>.jsonl)
  raw_bytes bigint NOT NULL,        -- observability: uncompressed size
  updated_at double precision NOT NULL DEFAULT extract(epoch from now()),
  PRIMARY KEY (channel, thread_ts),
  FOREIGN KEY (channel, thread_ts) REFERENCES sessions(channel, thread_ts) ON DELETE CASCADE);
ALTER TABLE transcripts ALTER COLUMN data SET STORAGE EXTERNAL;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS turn_seq bigint NOT NULL DEFAULT 0;
```

`ON DELETE CASCADE` makes retention a single delete: the sweep removes the session row and
the blob goes with it, in the same transaction. `STORAGE EXTERNAL` stops Postgres wasting
CPU attempting to pglz-compress bytes that are already gzipped.

### Ports

```python
@runtime_checkable
class TranscriptStore(Protocol):
    async def save(self, channel: str, thread_ts: str, sdk_session_id: str, data: bytes) -> None: ...
    async def load(self, channel: str, thread_ts: str, sdk_session_id: str) -> bytes | None: ...
```

The port carries **raw** bytes; gzip is a storage concern owned by the Postgres adapter, so
`MemoryStore` stays a plain dict and both adapters satisfy the shared behavior assertions in
`tests/store_behavior.py`.

`SessionStore` gains `async def bump_turn(channel, thread_ts) -> int` (returns the new
`turn_seq`), and `SessionRow` gains `turn_seq: int`.

A `LocalTranscripts` filesystem helper owns the one path formula (finding 3) and the
read/write/delete of the local `.jsonl`. It is injected, so tests use `tmp_path`.

### Turn flow

All of this runs inside the per-thread advisory lock jean already holds for the whole turn,
so there is never a second writer for a given transcript.

```
run_turn:
  row = store.get_session()
  if cached client and row.turn_seq != self._seen_seq:
      close()                          # another worker advanced this thread; our client is stale
  if no client:
      data = transcripts.load()        # cold start only: one read, ~33 KB
      local.write(sid, data)           # materialize the jsonl
      connect(resume=sid)              # finding 2: works on a virgin disk
  ... run the turn ...
  transcripts.save(local.read(sid))    # one write per turn, ≤85 KB
  self._seen_seq = store.bump_turn()
```

Hydration happens **once per cold client**, not once per turn — a warm pod pays nothing.

### The `turn_seq` fix (required for N>1, independent of storage)

`JeanSession` caches its `ClaudeSDKClient` across turns, and a cached client **skips
`_connect()` entirely** (`session/session.py:103`), so it never re-reads the store. Without
a guard: pod A runs turn 1 and caches its client; pod B serves turn 2 and advances the
transcript; message 3 returns to pod A, whose cached client has never seen turn 2 — it
answers from a divergent history and archives a stale blob **over** pod B's good one.
Silent memory corruption, only under N>1.

`turn_seq` closes this: every completed turn increments it, and a `JeanSession` whose
`_seen_seq` disagrees with the stored row drops its client and re-hydrates. This bug exists
in today's code and would exist under RWX too; it is not caused by this feature, but this
feature is what makes it reachable.

### Retention

Sessions and approvals are different data and get separate windows, replacing today's single
`cleanup_retention_days` applied to both:

| Setting | Default | Effect |
|---|---|---|
| `JEAN_SESSION_RETENTION_DAYS` | `3` | delete session rows idle ≥ 3 days; transcripts cascade |
| `JEAN_APPROVAL_RETENTION_DAYS` | `30` | unchanged: delete resolved approvals |
| `JEAN_CLEANUP_INTERVAL_HOURS` | `24` | was weekly — a 3-day window swept weekly lets rows live ~10 days |
| `JEAN_TRANSCRIPT_MAX_MB` | `32` | skip archiving (warn) above this raw size; guards the DB from a pathological thread |

`MaintenanceStore.prune` takes two cutoffs instead of one. The cross-worker
`try_claim_cleanup` gate is unchanged — exactly one worker prunes per interval.

**Accepted consequence:** the session row also carries `engaged` and `permission_mode`, so
deleting it at 3 days means a thread quiet that long needs a fresh @-mention to re-engage
jean, and any `acceptEdits` setting resets to default. Chosen deliberately over a two-tier
cool/delete scheme, for simplicity.

**Local disk** stays bounded: when `SessionManager` evicts an idle session it deletes that
pod's local `.jsonl` (Postgres has it), so a long-lived pod's disk tracks *live* threads
rather than every thread it ever served. Deletion is skipped if the last archive failed, so
a DB outage never destroys the only copy.

### Deployment

Remains a `Deployment` with `emptyDir` — no PVC, no StatefulSet, no Azure Files. Pods are
disposable; any pod serves any thread.

## Testing

Per CLAUDE.md: no live network, no live DB in the default run.

- `TranscriptStore` behavior assertions added to the shared `tests/store_behavior.py`, run
  against both `MemoryStore` and (skipped unless `JEAN_TEST_DATABASE_URL`) `PostgresStore`.
- Path formula: `LocalTranscripts.path()` reproduces the verified slug.
- Round trip: fake SDK client + `tmp_path` — archive after a turn, drop the client, hydrate,
  assert the resumed client was given `resume=<sid>` and the file was restored byte-for-byte.
- **Stale-client invalidation:** two `JeanSession`s over one `MemoryStore` (simulating two
  workers); the second runs a turn; assert the first drops its cached client and reconnects
  rather than reusing it.
- Retention: separate windows honored; pending approvals never pruned; deleting a session
  cascades its transcript.
- Gzip is an adapter detail: assert the Postgres adapter round-trips bytes, not that it
  compressed them.

## Out of scope (noted, not addressed)

Every thread shares one `cwd` (`~/.jean/workspaces`), so the CLI's `memory/` directory
inside that project dir is shared across all threads on a pod — a possible cross-thread
leak, unrelated to this work. Raised separately.

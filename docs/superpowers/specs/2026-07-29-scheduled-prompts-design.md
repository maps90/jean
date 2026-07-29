# Scheduled prompts

## Problem

Someone asks an agent for a recurring report — "send me a sprint summary every
Tuesday morning" — and the agent has no way to deliver it. jean has no scheduling
of any kind; `CleanupScheduler` prunes retention rows and is not user-facing.

Worse, the agent does not know that. Asked to schedule something it reaches for
the Claude Code `/schedule` skill, which targets claude.ai cloud routines and is
not part of jean's runtime. The call fails, the model reads the failure as
transient, and it promises to retry. It then tells the requester the report is
coming. Nothing arrives, and nobody finds out until the morning it doesn't.

The obvious workaround does not work either. A Slack reminder posting
"@agent post the summary" is authored by Slackbot, and the gateway drops every
message carrying a `bot_id` (`gateway/app.py:218,234`) — loop prevention, without
which agents would answer each other indefinitely. The same filter defeats any
external cron that posts through a bot token.

## What this builds

A schedule is a stored prompt, a cron expression, and a thread. When it comes due,
a worker injects the prompt as an ordinary turn in that thread. The agent answers
as it would to a human, and its reply lands in the thread as a reply.

Creating or removing a schedule requires human approval. Schedules can be paused
and resumed without losing them.

## Decisions

Each of these was chosen deliberately; the rejected option is recorded because the
reasoning is not recoverable from the code.

**Output replies in the originating thread**, not as a new channel post. The thread
is where the request was made, where the approval was granted, and where every
firing lands. A weekly post at channel level is noise for everyone not involved;
confining it to the thread means only the people already in that conversation see
it. The cost is that a threaded reply does not surface in the channel, so a
requester who never opens the thread will not see the output — accepted knowingly.

**Cron plus an IANA timezone**, not structured recurrence fields or a bare
interval. Models write cron fluently, one column covers everything from hourly to
"first Monday", and next-fire computation belongs to a library rather than
hand-rolled date arithmetic. A bare interval cannot express "Tuesday" at all: it
drifts the moment a run is late.

**Creation and removal go through the existing `ApprovalGate`.** A schedule causes
the agent to act autonomously later, which is exactly the class of thing the gate
exists for. The gate is called inside the tool, not left to the model's judgement —
an agent cannot write a row by declining to ask. Listing, pausing and resuming are
not gated: none of them create autonomous behaviour.

**A missed firing runs late within a grace window, then is skipped.** A summary
posted forty minutes late is fine; one posted two days late is confusing, and
carries a "weekly" framing that is no longer true. The miss is recorded rather
than swallowed.

**A firing does not touch engagement state.** The runner injects the turn below
the gateway, so `partner` is neither set nor cleared. A scheduled post never cuts
into a live exchange between two people and never leaves the agent believing a
conversation has just started with someone who did not speak.

**The session is the thread's existing session.** Because output goes to the
originating thread, the schedule needs no session identity of its own. The agent
retains history across firings and can say what changed since last week.

## Schema

```sql
CREATE TABLE IF NOT EXISTS schedules (
  id           TEXT PRIMARY KEY,
  channel      TEXT NOT NULL,
  thread_ts    TEXT NOT NULL,
  cron         TEXT NOT NULL,
  timezone     TEXT NOT NULL,
  prompt       TEXT NOT NULL,
  created_by   TEXT NOT NULL,
  enabled      BOOLEAN NOT NULL DEFAULT TRUE,
  next_run_at  TIMESTAMPTZ NOT NULL,
  last_run_at  TIMESTAMPTZ,
  last_status  TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS schedules_due ON schedules (next_run_at) WHERE enabled;
```

`last_status` is one of `ok`, `missed`, `error`. It is the only record of a firing
that did not produce output, and it is what makes a silently broken schedule
visible without reading logs.

**No foreign key to `sessions`, deliberately.** Retention prunes sessions after
`JEAN_SESSION_RETENTION_DAYS`, and a weekly schedule must outlive that window. A
cascade would delete schedules during routine cleanup — the failure would look
like schedules randomly disappearing weeks after anyone touched them. When a
firing finds no session, `handle()` starts a fresh one; the schedule keeps working
and loses only history.

## Components

| File | Responsibility |
|---|---|
| `ports.py` | `ScheduleStore` protocol: `create`, `list_for_thread`, `set_enabled`, `delete`, `claim_due` |
| `db/postgres.py` | asyncpg adapter |
| `db/memory.py` | in-memory fake implementing the same protocol |
| `schedule/cron.py` | `next_after(cron, tz, after) -> datetime`, and validation. Pure. |
| `schedule/runner.py` | `ScheduleRunner`: poll, claim, grace check, inject |
| `schedule/mcp.py` | agent tools, channel and thread bound at build time |
| `server.py` | wiring only |

`cron.py` is separated because it is pure and because it holds the fiddly cases —
DST boundaries, month rollover, malformed expressions. Isolating it makes those
testable with no store, no clock and no I/O.

## Tool surface

| Tool | Approval | Notes |
|---|---|---|
| `create(cron, timezone, prompt)` | required | channel and thread bound at build time |
| `list()` | no | schedules for the current thread only |
| `pause(id)` / `resume(id)` | no | `set_enabled`; creates no new autonomous behaviour |
| `remove(id)` | required | deletes the row |

`list`, `pause` and `resume` are ungated because none of them cause the agent to
act on its own: pausing only reduces what it does, and resuming restores a
schedule a human already approved. `create` and `remove` are gated because one
starts autonomous behaviour and the other destroys a record of it.

All five operate only on schedules belonging to the calling thread. An id from
another thread is treated as not found, so one thread cannot enumerate or cancel
another's schedules.

## Creating a schedule

```
human asks in thread
  -> agent calls mcp__jean_schedule__create(cron, timezone, prompt)
  -> tool validates cron + timezone, computes next_run_at
  -> tool calls ApprovalGate  ->  [Approve] [Deny] posted in the thread
  -> approved -> store.create(...) -> "scheduled, next run Tue 4 Aug 09:00 WIB"
  -> denied   -> nothing written
```

`channel` and `thread_ts` are bound when the tool is built, not passed as
arguments. The agent cannot create a schedule that fires into a thread other than
the one it is currently in.

Validation runs before the approval is raised, so no human is ever asked to
approve a schedule that cannot run.

## Firing

```
runner ticks every JEAN_SCHEDULE_POLL_SECONDS
  -> claim_due(now):
       SELECT ... WHERE enabled AND next_run_at <= now
       FOR UPDATE SKIP LOCKED
       advance next_run_at in the SAME transaction
  -> now - due > grace ?  -> log, last_status='missed', skip
  -> else -> SessionManager.handle(channel, thread_ts, prompt)
             -> ordinary turn under the thread lock
             -> agent replies in the thread
```

**`next_run_at` advances inside the claim transaction, before the turn runs.** A
worker that dies mid-turn loses that firing rather than re-firing on every restart.
This is the same trade the codebase already makes for `turn_seq`: losing one turn
beats corrupting a thread. Here, losing one summary beats a crash-loop that posts
the same summary repeatedly.

**`SELECT ... FOR UPDATE SKIP LOCKED`, not a global claim gate.** Schedules are
independent, so workers share the load, and two workers cannot claim the same row
even under clock skew. The `try_claim_cleanup` pattern would serialise every
schedule onto one worker for no benefit.

**Injection happens at `SessionManager.handle()`, below the gateway.** `handle()`
already acquires the thread lock (`session/manager.py:37`), so cross-worker
serialisation is inherited rather than rebuilt, and engagement — which lives in
the gateway above it — is untouched. This is what makes the "system turn"
semantics fall out of the existing layering instead of needing a bypass flag.

## Failure handling

| Failure | Behaviour |
|---|---|
| Invalid cron or timezone | Rejected in the tool; no approval raised, nothing written |
| A firing raises | Logged, `last_status='error'`; retries at the next occurrence, not immediately |
| DB unavailable at tick | Logged, retried next tick; nothing was claimed, so no state to reconcile |
| Session pruned by retention | Fresh session started in the thread; schedule unaffected |
| Collision with a human turn | Queues on the thread lock; neither dropped nor interleaved |

The runner loop catches per-schedule exceptions and continues, so one bad schedule
cannot stop the others — the same resilience `CleanupScheduler` already has.

## Testing

`cron.py`: weekly advance, month rollover, a DST boundary, and rejection of a
malformed expression. No fakes required.

`ScheduleRunner`: fake `ScheduleStore`, injected clock, fake session manager that
records calls. Fires when due; marks `missed` past the grace window; advances
`next_run_at`; never claims a disabled row; survives an exception mid-loop.

`schedule/mcp.py`: fake approval gate and fake store. The case that matters most is
**denied leaves nothing written**. Also: an invalid cron raises no approval at all,
and `create` uses the calling thread's own channel and `thread_ts` rather than
trusting arguments.

Postgres adapter: integration test in the existing style, skipped unless
`JEAN_TEST_DATABASE_URL` is set, so the default `pytest` run needs no database.

## Config

```
JEAN_SCHEDULE_POLL_SECONDS=30
JEAN_SCHEDULE_GRACE_SECONDS=3600
```

One new dependency, `croniter`, confined to `schedule/cron.py`.

## Out of scope

Auto-disabling a schedule after repeated failures, a per-thread schedule cap, and
catching up multiple missed occurrences. `last_status` makes failures visible; if a
dead schedule proves to be a real problem, that is the moment to add the guard.

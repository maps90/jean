# jean — Architecture

jean is a Slack-native Claude Code runtime: **one Slack thread = one persistent
claude-agent session**. It onboards an AI agent as a teammate in a Slack
workspace — the agent replies, edits, uploads, and reacts only through a
controlled Slack surface, and asks a human to approve before it does anything
irreversible. jean runs as **N identical stateless workers** behind a single
Slack app, with **Postgres** as the shared source of truth.

## Design principles

1. **Ports & adapters (hexagonal).** Domain logic depends only on interfaces
   (`typing.Protocol` ports), never on concrete Slack / SDK / database classes.
   Adapters implement those ports; the composition root wires them together.
2. **The LLM never makes a security decision (trust boundary).** The model may
   *project* the persona into typed data, but every Slack id it emits is
   re-validated against the raw persona file before any gate consumes it.
   Engagement, approver authorization, and permission all live in code.
3. **Stateless workers.** Correctness never depends on a message reaching the
   same worker twice. All durable state is in Postgres; sessions resume from a
   stored id; per-thread turns serialize with a database advisory lock;
   approvals coordinate across workers over Postgres LISTEN/NOTIFY.

## Layered structure

```
Slack (Socket Mode)  ── load-balances events across all connected workers ──┐
   │                                                                          │
   ▼                                                                          ▼
 worker 1                        …                                    worker N   (identical)
   │  events: app_mention, message, file_shared, slash cmds, block_actions
   ▼
 Gateway  (engagement + authorization — pure domain logic)
   │   ├─ SessionManager (per-worker cache) → JeanSession → ClaudeSDKClient(resume=…)
   │   ├─ ThreadLock port          (serialize turns per thread)
   │   ├─ jean_slack MCP tools      (reply / edit / upload / react / request_approval)
   │   ├─ MCP proxy → one stdio MCP server per *worker*, shared by every thread
   │   ├─ Persona (IDENTITY.md → typed SoulData, sha-cached; trust boundary)
   │   └─ ApprovalGate → ApprovalCoordinator port (LISTEN/NOTIFY)
   ▼
        ┌───────────── Postgres (shared: sessions, approvals) ─────────────┐
        │        NOTIFY 'jean_approvals' wakes the waiting worker           │
        └───────────────────────────────────────────────────────────────────┘
   +   filesystem ($JEAN_HOME): IDENTITY.md, per-thread workspaces, soul cache
```

- **Domain** (`gateway/`, `session/`, `approval/`, `persona/`) — the logic.
  It imports only ports and other domain modules. It never imports
  `slack_bolt`, `slack_sdk`, or `asyncpg`, and never constructs a
  `ClaudeSDKClient` directly.
- **Ports** (`ports.py`) — `SessionStore`, `ApprovalCoordinator`, `ThreadLock`,
  `ChatSurface`. Structural (`Protocol`) interfaces, so adapters satisfy them
  by shape and tests can use plain fakes.
- **Adapters** — `db/postgres.py` (asyncpg), `db/memory.py` (in-memory, for
  single-process runs and tests), `slack/client.py` (Slack Web API).
- **Composition root** (`server.py`) — the only place that builds concrete
  adapters (asyncpg pool, Slack client) and injects them into the domain.

## Module map

| Path | Responsibility |
|------|----------------|
| `config.py` | `JEAN_*` env → typed `Settings` (+ the two unprefixed auth tokens) |
| `ports.py` | Protocol interfaces: `SessionStore`, `ApprovalCoordinator`, `ThreadLock`, `ChatSurface` |
| `db/memory.py` | In-memory adapter for all data ports (single-process + tests) |
| `db/postgres.py` | asyncpg adapter: sessions, approvals, advisory locks, LISTEN/NOTIFY |
| `persona/model.py` | `SoulData` / `ApproverEntry` models + extraction prompt |
| `persona/identity.py` | Load `IDENTITY.md`; baseline + composed system prompt |
| `persona/extract.py` | Project persona → typed data; **trust-boundary grounding**; sha cache; regex fallback |
| `slack/mrkdwn.py` | Markdown → Slack mrkdwn conversion + chunking |
| `slack/client.py` | `ChatSurface` adapter over the async Slack Web API |
| `slack/mcp.py` | In-process `jean_slack` MCP tools the agent speaks through |
| `approval/authz.py` | Select authorized approvers by scope keyword match |
| `approval/gate.py` | Block Kit approval requests via the `ApprovalCoordinator` port |
| `session/session.py` | One resumable claude-agent turn loop per thread |
| `session/manager.py` | Per-worker session cache + `ThreadLock` serialization + idle sweep |
| `gateway/engagement.py` | Pure engagement decision (mention / disengage / DM / reply) |
| `gateway/dispatch.py` | Inbound message → attachment envelope → session turn |
| `gateway/app.py` | Gateway domain methods + the Slack event/action/command wiring |
| `plugins/mcp_stdio.py` | Spawn / reap a stdio MCP server child |
| `plugins/mcp_client.py` | One long-lived MCP server per worker: handshake, multiplexed calls, restart |
| `plugins/mcp_proxy.py` | Re-expose those servers' tools in-process, under their original tool ids |
| `plugins/mcp_config.py` | Which servers jean runs; takes a plugin's `.mcp.json` over from the CLI |
| `health.py` | `/healthz` (liveness) + `/readyz` (Postgres ping) |
| `server.py` | Composition root: pool, adapters, MCP servers, socket-mode, sweeper |

## Request flow (happy path)

1. A user `@`-mentions jean in a thread. Slack (Socket Mode) delivers the event
   to one worker.
2. The Gateway decides engagement, acquires the thread's advisory lock, and
   hands the turn to `SessionManager`.
3. `JeanSession` opens (or resumes, via the stored `sdk_session_id`) a
   `ClaudeSDKClient`, sets a "thinking…" status, and feeds the message.
4. The agent works; to speak it calls `mcp__jean_slack__reply`. Before any
   mutating action it calls `mcp__jean_slack__request_approval`.
5. The approval is persisted and posted as Block Kit buttons. Whichever worker
   receives the click resolves it in Postgres and fires `NOTIFY`; the waiting
   worker wakes and the turn continues.
6. After the turn, the (possibly new) session id is persisted. Later the idle
   sweep may drop the client; the next message resumes from Postgres.

**Resume is best-effort, and Postgres alone cannot make it otherwise.** Postgres
stores the session *id*; the `claude` CLI stores the *transcript* that id names on
local disk (`$HOME/.claude/projects/<cwd>/<id>.jsonl`). A restarted pod — or any
other replica — therefore resumes an id whose transcript it cannot see, and the CLI
exits 1 during startup ("No conversation found with session ID"). `JeanSession`
handles that by reconnecting without `resume`: the thread keeps working and loses
the agent's memory of its earlier turns, and says so in-thread. Threads survive a
restart; their history does not.

To make resume durable, mirror transcripts into Postgres via the SDK's
`ClaudeAgentOptions.session_store` hook (`append`/`load`; `load` materializes the
transcript before subprocess spawn, so it needs no shared filesystem). Not built.

## Persistence

Postgres holds two tables (created idempotently at boot):

- `sessions(channel, thread_ts, sdk_session_id, permission_mode, engaged,
  last_active_at, PRIMARY KEY (channel, thread_ts))`
- `approvals(id, channel, thread_ts, summary, status, approved, approver_id,
  approvers, requested_at, resolved_at)`

Per-thread serialization uses `pg_advisory_xact_lock(hashtext(channel||':'||thread))`
inside a transaction (auto-released on commit). Cross-worker approval wake-ups
use `pg_notify('jean_approvals', <id>)` with a dedicated `LISTEN` connection on
the waiting side. The in-memory adapter mirrors these semantics so both are
proven against the same behavioral test suite.

## Persona & the trust boundary

`IDENTITY.md` (under `$JEAN_HOME`) is the operator-authored persona: identity,
manager, mandate, values, and approver entries with described scopes. At boot an
ephemeral extraction pass projects it into typed `SoulData`, sha-cached. Every
Slack id in the result **must appear verbatim in the raw `IDENTITY.md`** or it is
dropped/rejected — so a jailbroken persona can mislead an approver but can never
invent an approver, redirect messages, or self-approve. Approver selection is a
pure function over the grounded data; the agent supplies only a plan summary,
never user ids.

## Authentication

Exactly one of:

- `ANTHROPIC_API_KEY` — API billing (wins if both are set).
- `CLAUDE_CODE_OAUTH_TOKEN` — a subscription token from `claude setup-token`.

The agent SDK inherits the token from the process environment. The extraction
pass authenticates directly: API key, or OAuth Bearer with the OAuth beta header.

## Testing

Domain and adapters depend on ports, so the default suite runs entirely against
the in-memory adapter and fakes — no Postgres, Slack, or network required. The
Postgres adapter has its own integration test, gated on `JEAN_TEST_DATABASE_URL`
(CI provides a Postgres service), and reuses the same behavioral assertions as
the in-memory adapter so the two stay equivalent.

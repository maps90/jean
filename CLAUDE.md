# CLAUDE.md — jean

Read this before writing any code in this repo. It is the contract every task and every agent follows.

## What jean is

A **Slack-native Claude Code runtime**: one Slack thread = one persistent claude-agent session. jean is the AI teammate. The agent speaks to Slack only through an in-process MCP server and asks a human to approve before mutating anything.

Architecture: `ARCHITECTURE.md`

## Architecture pattern: ports & adapters (hexagonal)

The codebase is built so pieces are **easy to swap** without rewriting domain logic, and so
it runs as **N stateless workers** behind one Slack app from day one.

- **Domain / core** — `gateway/`, `session/`, `approval/` (gate + authz), `persona/` — holds
  the logic. It depends ONLY on interfaces (ports), never on concrete Slack/SDK/DB classes.
- **Ports** — `src/jean/ports.py` — `typing.Protocol` interfaces for each boundary:
  `SessionStore`, `ApprovalCoordinator`, `ThreadLock`, `ChatSurface`, `AgentClient`. Python
  Protocols are *structural*: a concrete class satisfies a port just by having the methods —
  no inheritance needed, so adapters don't import the port and tests use plain fakes.
- **Adapters** — `db/postgres.py` (asyncpg), `slack/client.py` (Slack), the SDK client —
  concrete implementations of the ports. In-memory fakes (`db/memory.py`) implement the same
  ports for single-process runs and tests.
- **Composition root** — `server.py` is the ONLY place that constructs concrete adapters
  (asyncpg pool, Slack client, coordinator) and wires them into the domain. Everything else
  receives its collaborators by injection (constructor args / factory callables).

**Layering rule (enforced in review):** a module in `gateway/`, `session/`, `approval/`,
or `persona/` must NOT `import` `slack_bolt`, `slack_sdk`, `asyncpg`, or construct a
`ClaudeSDKClient` directly. Those live behind ports and are injected. `server.py` and the
`db/`+`slack/` adapters are the only places that touch concrete infra.

## Scaling model: stateless workers + Postgres

jean runs as one or more identical worker processes behind a single Slack app. Correctness
never depends on a message reaching the same worker twice.

- **Shared state in Postgres** (`asyncpg` pool): sessions (thread ↔ `sdk_session_id`,
  permission_mode, engagement), and approvals. No durable state in process memory; the
  in-process session map is only a best-effort per-worker cache.
- **Sessions are resumable, but only best-effort:** any worker handles any message for a
  thread by creating a `ClaudeSDKClient` with `resume=<stored sdk_session_id>`. A worker may
  drop a client after idle; the next message (on any worker) resumes from Postgres.
  Postgres holds the session *id*, though — the CLI holds the *transcript* it names, on
  local disk. A restarted pod or a second replica resumes an id it cannot see, and the CLI
  exits 1 at startup. So `JeanSession` falls back to a fresh session (no `resume`) when a
  resume fails, and only when a resume fails: a startup failure that is not about the resume
  id (bad `--plugin-dir`, bad auth) exits 1 identically, so the two are told apart by
  outcome — if connecting *without* `resume` also fails, it was never the resume; propagate
  and leave the stored id alone. Never distinguish them by parsing the CLI's stderr.
- **Per-thread serialization across workers** via the `ThreadLock` port. The Postgres adapter
  uses `pg_advisory_xact_lock(hashtext(channel||':'||thread))` so two messages for one thread
  never run concurrently, even on different workers. Single-process fallback = `asyncio.Lock`.
- **Cross-worker approvals** via the `ApprovalCoordinator` port. `request_approval` (running
  on worker A) persists a pending row and `await`s the coordinator; the Approve/Deny click —
  which Socket Mode may route to any worker B — calls `resolve(...)`, writing the decision and
  firing Postgres **`NOTIFY`** on channel `jean_approvals`. Worker A holds a **`LISTEN`**
  connection, wakes on the notification, reads the decision, and continues. A short poll on
  the row is the safety net for a missed notification only if one is added later; the primary
  path is LISTEN/NOTIFY.

Do not reintroduce in-memory-only coordination (e.g., resolving an `asyncio.Future` that only
exists on one worker) — it breaks the moment a second replica runs.

## The trust boundary (non-negotiable)

The LLM never makes a security decision. Extraction may *project* `IDENTITY.md` into typed
`SoulData`, but every Slack id it returns MUST appear verbatim in the raw `IDENTITY.md` text
before any gate uses it (`assert_ids_grounded`); ungrounded ids are dropped/rejected.
Engagement, approver authorization, and permission all live in gateway code, never in a
prompt. A jailbroken persona can mislead an approver but cannot redirect messages or
self-approve.

## Conventions

- **Python 3.11+**, `from __future__ import annotations` at the top of every module; modern
  type hints (`str | None`, `list[str]`).
- **Async everywhere** on I/O paths. Domain methods that touch a port are `async`.
- **Dependency injection over globals.** No module-level singletons for stateful things;
  pass collaborators in. This is what makes the code testable and swappable.
- **One responsibility per file.** If a file outgrows its purpose, that's a signal to split —
  raise it, don't silently grow a 500-line module.
- **Naming** describes what a thing does, not how. Ports are nouns (`SessionStore`), adapters
  name their tech (`SlackSurface`), domain services name their role (`Gateway`, `ApprovalGate`).
- **Errors:** never swallow silently in domain code. The one deliberate exception is
  best-effort Slack niceties (status/reactions) where the scope may be absent — those catch
  and continue, with a comment saying why.
- **Config** via `JEAN_*` env → `Settings` (pydantic-settings). The two auth tokens are the
  only unprefixed env vars.

## Auth (both modes must work)

Exactly one of:
- `ANTHROPIC_API_KEY` — API billing. **Wins if both are set** (explicit > subscription).
- `CLAUDE_CODE_OAUTH_TOKEN` (`sk-ant-oat01-…`) — Claude Pro/Max subscription. Generate with
  `claude setup-token` on a logged-in machine.

The `claude-agent-sdk` child auto-detects `CLAUDE_CODE_OAUTH_TOKEN` from the **process env**
(inherited — no wiring). The soul extractor calls the `anthropic` SDK directly, so it must
branch: API key → `AsyncAnthropic(api_key=…)`; OAuth only → `AsyncAnthropic(auth_token=…,
default_headers={"anthropic-beta": "oauth-2025-04-20"})` or the API returns 401.

## Testing

- **TDD**: failing test → run (see it fail) → minimal code → run (pass) → commit.
- **No live network in tests.** Inject fakes at the ports: fake web client, fake SDK client,
  fake extractor, `tmp_path` sqlite. Tests assert real behavior, not that a mock was called
  with args you also hardcoded.
- `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"` — no `@pytest.mark.asyncio` needed).
- Test output must be pristine — no stray warnings.

## Quality gate & workflow

- Before every commit run **`./scripts/verify.sh`** (ruff check + ruff format-check + pytest).
  CI (`.github/workflows/ci.yml`) runs the same gate.
- Fix lint with `uv run ruff check --fix src tests` and `uv run ruff format src tests`.
- **Small, frequent commits** — one per plan task, using the plan's commit message.
- Do NOT add AI co-author trailers to commits in this repo.

### One feature at a time, in its own worktree

- **Every new feature or bugfix starts in a git worktree**, never directly in the primary
  checkout. Create it before the first edit:
  `git worktree add ../jean-<slug> -b <slug>` (branch off `main`), then work there.
  When the branch is merged or abandoned, remove it: `git worktree remove ../jean-<slug>`.
  The primary checkout stays on a clean `main` so a review, a hotfix, or a `kubectl`-adjacent
  debug session never collides with in-progress work.
- **No parallel work.** Do one task at a time, start to finish, before picking up the next.
  Do not fan out subagents to edit code concurrently, do not run multiple features in flight,
  and do not split a plan across parallel agents — sequential execution only. Read-only
  exploration may be delegated, but writes come from one worker, one branch, one worktree.
- **Why:** jean's correctness lives in ordering (per-thread locks, LISTEN/NOTIFY handoffs,
  the plugin MCP takeover). Interleaved edits from concurrent agents produce diffs no one can
  reason about and a verify gate no one can trust.

## SDK & Slack gotchas (verified against claude-agent-sdk 0.2.110 / slack-bolt 1.29.0)

- In-process MCP tools surface as `mcp__jean_slack__<tool>` (server key `jean_slack`). List
  them in `allowed_tools`.
- **jean owns the external MCP servers, not the CLI.** stdio transport *is* a child process,
  so a per-session `ClaudeSDKClient` cannot share one — hand the CLI a stdio server (in
  `mcp_servers`, or in a plugin's `.mcp.json`) and it forks its own copy of it for *every*
  session. That is what exhausted the pod's memory. Instead `plugins/mcp_client.py` runs each
  server once per worker and `plugins/mcp_proxy.py` re-exposes its tools as an in-process SDK
  server, keyed so the tool ids are unchanged (`mcp__plugin_kubectl_kubernetes__pods_list`).
  `take_over_plugin_mcp()` renames a plugin's `.mcp.json` so the CLI cannot spawn it behind
  jean's back. Never put a stdio server config into `ClaudeAgentOptions.mcp_servers`.
- `@tool(name, description, input_schema)` wraps an **async** `fn(args: dict) -> dict` that
  returns `{"content": [{"type": "text", "text": …}], "is_error"?: True}`. Keep each tool's
  logic in a plain async function so it's unit-testable without the SDK wrapper.
- Persist `ResultMessage.session_id` after each turn; resume via `ClaudeAgentOptions(resume=…)`.
- The SDK shells out to the Claude Code CLI at runtime (`CLINotFoundError` if absent) — that's
  why tests use a fake client, never a real one.
- Slack: `reactions_add(channel=, name=<no colons>, timestamp=<msg ts>)`;
  `assistant_threads_setStatus(channel_id=, thread_ts=, status=)` (note `channel_id`).
  slack-bolt async needs `aiohttp` (via the `[async]` extra).
- **mrkdwn conversion order matters:** carve code/urls out first, convert italic BEFORE bold
  (single-star italic must not eat `**bold**`), restore last. See `slack/mrkdwn.py`.

## Postgres gotchas

- `asyncpg` uses positional `$1, $2` placeholders (not `%s`) and returns `Record` objects.
- Schema is created idempotently at boot (`CREATE TABLE IF NOT EXISTS`) via the pool.
- `LISTEN/NOTIFY`: `pg_notify('jean_approvals', payload)` from the resolver; the waiter uses a
  dedicated connection's `add_listener('jean_approvals', cb)`. Payload = the approval id.
- Advisory locks: use the *xact* variant inside a transaction so the lock auto-releases on
  commit/rollback — never leak a session-level advisory lock.
- **Testing without a DB:** domain logic depends on the ports, so unit tests use the in-memory
  fakes (`db/memory.py`) — fast, no DB. The asyncpg adapter has its own integration test that
  is **skipped unless `JEAN_TEST_DATABASE_URL` is set** (CI provides a Postgres service). Never
  require a live Postgres for the default `uv run pytest`.

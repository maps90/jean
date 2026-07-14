# jean

jean is a Slack-native Claude Code runtime: **one Slack thread = one persistent
claude-agent session**. jean is your AI teammate -- it lives in Slack, speaks
only through an in-process MCP server, and asks a human to approve before it
mutates anything outside the conversation. It runs as N stateless workers
behind a single Slack app with Postgres as the shared state.

See `ARCHITECTURE.md` for the design (ports & adapters, trust boundary,
stateless-worker model) and `CLAUDE.md` for the working conventions.

## Quick start

```bash
uv sync
```

### 1. Give jean an identity

Create `~/.jean/IDENTITY.md` (or set `JEAN_HOME` to point elsewhere). This is
a plain-text persona document -- jean's system prompt is composed from it.
At minimum, tell it who its manager is and who can approve actions, using
real Slack mentions (every id jean uses at runtime must appear verbatim in
this file -- see "Trust boundary" below):

```markdown
# jean

I am jean, an AI teammate for the engineering team.

My manager is <@U0123ABCD>. I take direction from them and keep them
informed of anything important.

Approvers:
- <@U0456EFGH> approves deploys and infra changes (scope: deploy, release, infra)
- <@U0123ABCD> is the catch-all approver for anything else

I live in #eng-jean and can be DMed directly.
```

Replace the `<@U...>` ids with real Slack user ids (Slack shows these when
you right-click a user's name -> "Copy member ID", or via a mention
autocomplete in a message draft).

### 2. Configure environment

Copy `.env.example` to `.env` and fill in:

| Variable | Required | Notes |
|---|---|---|
| `JEAN_SLACK_BOT_TOKEN` | yes | `xoxb-...`, from your Slack app's OAuth page |
| `JEAN_SLACK_APP_TOKEN` | yes | `xapp-...`, Socket Mode app-level token |
| `ANTHROPIC_API_KEY` | one of these two | pay-as-you-go API billing; **wins if both are set** |
| `CLAUDE_CODE_OAUTH_TOKEN` | one of these two | use a Claude Pro/Max subscription instead of API billing. Generate on a machine already logged into Claude Code: `claude setup-token`, then paste the `sk-ant-oat01-...` token here |
| `JEAN_DATABASE_URL` | yes (or rely on the compose default) | `postgresql://user:pass@host:5432/db` |
| `JEAN_HOME` | no | defaults to `~/.jean` |
| `JEAN_APPROVERS` | no | comma-separated Slack user ids (`U11111,U22222`) used **only** when `IDENTITY.md` names nobody who can approve a given action. A safety net for a soul that fails to parse -- see "Who gets asked" below |
| `JEAN_IDLE_MINUTES`, `JEAN_APPROVAL_TTL`, `JEAN_PERMISSION_MODE`, `JEAN_HEALTH_PORT`, `JEAN_MODEL`, `JEAN_SOUL_PARSE_MODEL` | no | see `.env.example` for defaults |
| `JEAN_CLEANUP_ENABLED` | no | Postgres retention cleanup (default: `true`). One worker per cycle prunes, via an advisory lock. |
| `JEAN_SESSION_RETENTION_DAYS` | no | delete sessions idle longer than this (default: `3`). The session's transcript cascades away with the row — **and so do `engaged_with` and `permission_mode`**, so a thread that has been quiet this long needs a fresh `@jean` mention to re-engage. |
| `JEAN_APPROVAL_RETENTION_DAYS` | no | delete resolved approvals older than this (default: `30`) — the audit trail outlives the memory. Pending approvals are never pruned. |
| `JEAN_CLEANUP_INTERVAL_HOURS` | no | how often the sweep runs (default: `24`) |
| `JEAN_TRANSCRIPT_MAX_MB` | no | refuse to archive a transcript bigger than this (default: `32`), rather than let one pathological thread bloat the database. That thread keeps working, but only on the worker holding it — it won't resume elsewhere. |
| `JEAN_SETTLE_QUIET`, `JEAN_SETTLE_INTERVAL`, `JEAN_SETTLE_TIMEOUT` | no | the CLI writes a turn to its transcript *write-behind*, so jean waits for the file to stop changing before archiving it (defaults, in seconds: `1.0` of quiet, sampled every `0.1`, capped at `10.0`). Too short a quiet window archives a turn whose answer the CLI has not written yet — a cold worker then resumes a hole. |

Exactly one of `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` must be set --
these two are the only unprefixed env vars, everything else is `JEAN_*`.

### 3. Slack app setup

Create a Slack app (Socket Mode, not HTTP events) with these OAuth scopes:

- `app_mentions:read` -- so jean notices `@jean` mentions
- `chat:write` -- to post/edit replies
- `im:history` -- to see DMs
- `files:write` -- to upload files
- `reactions:write` -- to react/unreact
- `assistant:write` *(optional)* -- shows the "is thinking..." status in
  Slack's AI-assistant UI; jean degrades gracefully (silently) without it

Enable Socket Mode and generate an app-level token (`xapp-...`) with the
`connections:write` scope, plus a bot token (`xoxb-...`) from the OAuth page.
Install the app to your workspace, invite the bot to whatever channels it
should watch, and optionally DM it directly.

### 4. Run it

Locally:

```bash
uv run jean
```

With Docker Compose (includes a Postgres service):

```bash
docker compose up
```

### 5. Give jean tools: MCP servers

`mcp.json` (at `$JEAN_HOME/mcp.json`, or wherever `JEAN_MCP_CONFIG_PATH` points --
mount it from a Secret in production) is where you tell jean about the tools it has.
Two kinds of entry:

- **stdio** (has a `command`) -- jean runs the server itself, once per worker, and
  proxies its tools in-process. Never let the CLI spawn these; see `ARCHITECTURE.md`.
- **remote** (has a `url`, no `command`) -- an HTTP/SSE server jean does not run. The
  CLI connects to it directly.

**Register every HTTP API that speaks MCP.** A server jean knows about surfaces as real
tools (`mcp__portico__…`) with real schemas, and those are auto-allowed -- the agent
calls them without an approval click. An HTTP API jean is *not* told about gets reached
with `curl` through `Bash` instead, and **every `Bash` call costs a human an approval
click**. The difference is not cosmetic: one unregistered MCP server turned a single
"list our open Jira tickets" question into eleven Approve clicks in four minutes, with
the agent guessing at tool names it had no schema for.

```json
{
  "mcpServers": {
    "portico": {
      "type": "http",
      "url": "https://portico.int.okadoc.net/mcp",
      "headers": { "Authorization": "Bearer ${PORTICO_ACCESS_TOKEN}" }
    }
  }
}
```

**A plugin can declare its own servers**, in its `.mcp.json`, and jean registers them the
same way -- stdio ones it runs and proxies, http ones it hands to the CLI. You do not
copy a plugin's server into `mcp.json` by hand. A plugin's http server is keyed by the
name it declares (so `"portico"` → `mcp__portico__…`), and two servers claiming the same
name is a boot error, because that name is the tool prefix and one would shadow the other.

`${VAR}` and `${VAR:-default}` are read from jean's environment, so a credential stays in
the environment (Vault → env, like every other jean secret) instead of being copied into
the mounted config. **A `${VAR}` that is unset or empty, with no default, is a boot
failure** -- not a warning. jean refuses to start rather than send an empty credential,
401 on every call, and leave the agent to fall back to `curl`: straight back to the click
storm.

> **A registered server's tools are ALL auto-allowed, writes included.** Registering an
> Atlassian MCP server means jean can file a Jira ticket with no button click. Register
> only servers whose token is scoped to what jean should be allowed to do -- the token is
> the blast radius, because the approval gate is not in this path.

## Using jean in Slack

- **Mention it** (`@jean ...`) in any channel it's in, or **DM it directly**
  -- either makes you jean's conversation partner in that thread.
- As the partner, your plain follow-ups in the same thread keep getting
  answered, no need to re-mention. Anyone else's messages are never
  delivered to jean at all -- no turn, no tokens, no database write --
  until they @-mention her themselves and take over as partner.
- Only the partner can step jean back, by mentioning someone else
  (`@budi can you take this?`); that hands the thread off and jean stays
  quiet until she's mentioned again. That, or the thread goes idle.
- `/mode <mode>` -- set this thread's permission mode (`default`,
  `acceptEdits`, `plan`, `bypassPermissions`, `dontAsk`, `auto`).
- `/help` -- list available commands.
- Before jean does anything that mutates something outside the conversation,
  it posts a Block Kit approval request and waits for an authorized approver
  to click Approve/Deny (or for the request to time out and auto-deny).

### Who gets asked

Only the people jean picks here may click Approve/Deny -- a click from anyone
else is rejected. The set is chosen in code (never by the model), taking the
first rung that yields anyone:

1. **Scope match** -- every approver whose `scope` keywords appear in the
   action's summary (`<@U0456EFGH> ... (scope: deploy, release, infra)`).
2. **Catch-all approver** -- anyone `IDENTITY.md` marks as approving anything
   ("is the catch-all approver for anything else").
3. **`JEAN_APPROVERS`** -- the env-level backstop.
4. **The manager** -- the person `IDENTITY.md` says jean answers to.

If all four are empty, jean **refuses the action** and says so in the thread.
It does not post buttons in that case: with nobody authorized, every click
would be rejected and the request would simply hang until it timed out. Give
jean at least a manager or a catch-all approver.

## Horizontal scaling

jean workers are stateless -- all session and approval state lives in
Postgres (thread <-> `sdk_session_id`, permission mode, pending approvals).
Any worker can handle any message for any thread; turns on the same thread
are serialized via a Postgres advisory lock, and approvals resolved on one
worker wake up whoever is waiting on another worker via `LISTEN`/`NOTIFY`.
Scale by running more replicas behind the same Slack app:

```bash
docker compose up --scale jean=3
```

## Trust boundary

jean's persona (`IDENTITY.md`) is projected into structured data (manager,
approvers, allowed channels, etc.) by an LLM extraction step, but **the LLM
never makes a security decision**. Every Slack id that extraction produces
must appear verbatim in the raw `IDENTITY.md` text before any code path
trusts it (`persona/extract.py::assert_ids_grounded`); anything invented is
rejected in favor of a deterministic regex fallback. Approver authorization
and engagement rules live in plain Python (`approval/authz.py`,
`gateway/engagement.py`), never in a prompt -- a jailbroken persona can
mislead a human approver, but it cannot redirect messages to a different
channel/user or approve its own actions.

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
| `JEAN_IDLE_MINUTES`, `JEAN_APPROVAL_TTL`, `JEAN_PERMISSION_MODE`, `JEAN_HEALTH_PORT`, `JEAN_MODEL`, `JEAN_SOUL_PARSE_MODEL` | no | see `.env.example` for defaults |
| `JEAN_CLEANUP_ENABLED`, `JEAN_CLEANUP_RETENTION_DAYS` | no | weekly Postgres retention cleanup; prunes resolved approvals + idle sessions older than the window (default: enabled, 30 days). One worker prunes per week via an advisory lock. |

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

## Using jean in Slack

- **Mention it** (`@jean ...`) in any channel it's in, or **DM it directly**
  -- both engage it in that thread.
- Once engaged, jean keeps responding to follow-ups in the same thread until
  someone else is mentioned instead (jean steps back) or the thread goes
  idle.
- `/mode <mode>` -- set this thread's permission mode (`default`,
  `acceptEdits`, `plan`, `bypassPermissions`, `dontAsk`, `auto`).
- `/help` -- list available commands.
- Before jean does anything that mutates something outside the conversation,
  it posts a Block Kit approval request and waits for an authorized approver
  to click Approve/Deny (or for the request to time out and auto-deny).

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

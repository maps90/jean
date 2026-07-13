# Remote MCP servers: env expansion, so Portico stops arriving as `curl`

## The problem, as it showed up

An SRE thread asked jean for open Jira tickets. Answering it cost **six Slack approval
clicks in two minutes** — and the seventh was still pending when the human gave up:

```
Approved  Run a shell command — List available Portico MCP tools
Approved  Run a shell command — List Portico MCP tools with correct Accept header
Approved  Run a shell command — List all Portico MCP tool names
Approved  Run a shell command — Search open SRE Jira tickets via Portico MCP
Approved  Run a shell command — Get Atlassian cloud resources to find cloudId
Approved  Run a shell command — Check Portico connection status for Atlassian
Pending   Run a shell command — Check Atlassian user info via Portico
```

Every one of those is a `curl` to `https://portico.int.okadoc.net/mcp` — hand-written
JSON-RPC, with a `python3 -c` snippet bolted on to parse the reply.

**This is not an approval-policy problem. It is a registration problem.** jean's permission
model has two buckets (`agent_options.py:41-73`): a registered MCP server's tools are
auto-allowed (`allowed_tools` gets `mcp__<server>__*`), and everything else — `Bash`,
`Write`, `Edit` — goes to the human gate, one button per call, with no read-only exception.

Portico *is* an MCP server. jean just doesn't know that. It is absent from `mcp.json`, so
the agent reaches an HTTP endpoint with the only tool it has for one: `Bash`. And a `Bash`
always asks. Six prompts for what is two tool calls.

Nothing is wrong with Portico. It is a conformant streamable-HTTP MCP server — jean is
speaking correct JSON-RPC to it and getting correct answers back.

## The fix

Register Portico as a remote MCP server. `remote_servers()` (`plugins/mcp_config.py:63`)
already takes any `mcp.json` entry without a `command` and hands it to the CLI untouched;
`proxy_tool_patterns` already puts `mcp__portico__*` into `allowed_tools`. So the deployment
side is a config change in flux-infra's mounted `mcp.json`:

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

Portico's tools become first-class — `mcp__portico__atlassian__searchJiraIssuesUsingJql` —
with typed schemas, no curl, no `python3 -c` parsing, and **zero approval clicks**.

### What this repo has to change

One thing: **`${VAR}` is not expanded in a remote server's config.**

`expand()` (`plugins/mcp_stdio.py:40`) exists, but is only applied to a *stdio* server's
`env` block. `remote_servers()` passes its configs through verbatim. So
`"Bearer ${PORTICO_ACCESS_TOKEN}"` may reach Portico as that literal string.

Whether the CLI would expand it for us is beside the point — the SDK ships these servers as
an inline `--mcp-config` JSON blob (`subprocess_cli.py:326`), not as an on-disk `.mcp.json`,
and jean should not be betting a bearer token on undocumented CLI behaviour. Expanding in
jean is idempotent: if the CLI also expands, there is nothing left for it to find.

**Change:** `remote_servers()` expands `${VAR}` across the string values of each remote
config (recursively — `url`, and every value in `headers`).

The alternative — writing the literal token into the mounted `mcp.json` Secret — is rejected:
`PORTICO_ACCESS_TOKEN` already arrives from Vault as an env var like every other jean secret,
and a second copy of a credential in a second place is a second thing to rotate.

### An unset variable fails at boot, loudly

`expand()`'s stdio behaviour is to substitute `""` for a variable that is not set. Inherited
as-is, an unset `PORTICO_ACCESS_TOKEN` would send `Authorization: Bearer ` — jean boots
clean, registers the server, and then *every Portico call 401s* for a reason nothing in the
logs explains. The agent, finding its tools broken, would very plausibly fall back to `curl`
— straight back to the click storm this spec exists to end.

So a remote config referencing an **unset** env var raises at boot. This matches how jean
already treats a malformed `JEAN_APPROVERS` (`config.py:93`): a credential misconfiguration
is a deploy-time error, caught on the deploy that caused it, not a mystery at 2am.

Note the asymmetry is deliberate — stdio's silent-empty behaviour is left alone. Changing it
is out of scope, and a stdio server that dies from a missing var dies visibly, at spawn, with
its stderr captured (`stderr_tail`).

## Explicitly accepted: this auto-allows Portico's writes

Registering Portico means `createJiraIssue`, `editJiraIssue`, `transitionJiraIssue` and
`createConfluencePage` are auto-allowed too. jean can file a Jira ticket with no button click.

This is accepted, knowingly. It is already the posture for the `kubectl` plugin — every tool
it exposes, destructive ones included, is auto-allowed today. Portico does not open a new
hole; it makes an existing one visible. jean's blast radius here is bounded by what the
Portico token is scoped to.

Making the gate tool-aware — so an MCP *write* asks and an MCP *read* does not — is real
work and a separate spec. It is not in this one.

## Out of scope

- A read-only allowlist for `Bash` (shell classification is a security minefield).
- Sticky / batched approvals ("approve once, don't ask again").
- Tool-aware gating of MCP writes (see above).
- Anything in Portico itself.

## Testing

`remote_servers()` is a pure function over a dict, so this is unit-testable with no network
and no DB — `monkeypatch.setenv` and assert:

1. `${VAR}` in a header value is replaced with the env var's value.
2. `${VAR}` in `url` is replaced.
3. A config with no `${...}` in it is returned unchanged (byte-for-byte).
4. A referenced-but-unset var raises, and the error names the variable and the server.
5. stdio entries are still filtered out of `remote_servers()`, and `stdio_servers()` still
   expands nothing it did not expand before (no regression).

## Follow-up, outside this repo

- **flux-infra:** add the `portico` block above to jean's mounted `mcp.json`.
- **oka-skills, `portico` skill:** this is where the `curl https://portico…/mcp` pattern is
  taught. It must be rewritten to call the `mcp__portico__*` tools. Left in place, jean will
  keep curling out of habit even once the real tools are sitting right next to it — and every
  one of those curls is an approval click, so the skill alone can undo this whole change.

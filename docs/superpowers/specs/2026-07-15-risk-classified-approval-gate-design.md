# Risk-classified approval gate

**Date:** 2026-07-15
**Status:** Design approved, ready for planning
**Branch:** `risk-classified-gate`

## Problem

The shipped `plan-then-approve` flow gates **every mutating turn** behind a plan
approval click. Even a routine, low-risk change (edit a workspace file, run
tests, a local `git commit`) forces a human to click before jean proceeds. The
gate acts as a blocker on ordinary work.

The goal: **the gate should only interrupt for genuinely risky or unusual
actions.** If a turn does nothing sensitive, no human should be pulled in at
all. Routine work runs gateless; the gate stops being a blocker and becomes an
exception handler.

## Non-negotiable constraint (from CLAUDE.md)

> The LLM never makes a security decision.

Therefore "is this risky?" **cannot** be a question asked of the model. Risk
must be decided by deterministic code that inspects the *actual* tool call
`(tool_name, tool_input)` — never a model-supplied paraphrase, never the free
text of a plan. This is why the decision lives in the SDK's `can_use_tool`
hook (real call, real args) and not on `ExitPlanMode` (model-authored plan
text).

## Reference: how slaude solves the same problem

slaude (the same author's TypeScript sibling) does **not** use plan mode as its
gate. It runs permissive and gates per-tool, with two escape valves that keep
it quiet:

- **`PermissionGate`** — the SDK `canUseTool` hook. It deterministically
  auto-allows safe namespaces (its own surface/runtime tools, read-only
  introspection, an env allowlist) with no prompt; hard-denies the
  SDK-synthesized OAuth bootstrap tools in code; and prompts on everything else
  with **three** buttons: `Allow once` / `Always allow` / `Deny`. "Always allow"
  writes a **session-scoped `addRules` permission** (honoring the SDK's
  suggested pattern, e.g. `Bash(ls:*)`), so it never re-asks for that pattern.
- **`ApprovalGate`** — an agent-initiated, per-task checkpoint the persona
  triggers before destructive batches while the agent otherwise runs free.

jean already has the equivalent of slaude's second gate: the agent-driven
`request_approval` tool in `slack/mcp.py`. This design brings jean's *per-tool*
path in line with slaude's `PermissionGate`, and sharpens "what prompts" with an
explicit risk classifier instead of slaude's coarser namespace-only allowlist.

## Design

### 1. Default permission mode: `plan` → `default`

`config.py`: `permission_mode` default changes from `"plan"` to `"default"`.

The agent is no longer forced read-only. Under `default` mode the CLI calls
`can_use_tool` for every tool it will not auto-allow — i.e. everything outside
`allowed_tools` (jean's own Slack tools + the MCP proxy patterns already listed
there) and outside the CLI's read-only set. So Bash / Write / Edit / mutating
plugin-MCP calls reach the hook; jean's Slack posting and `kubectl get` do not.
`plan` remains a valid value a thread may select via `/mode`; it is simply no
longer the default and no longer wired to a single-approval flip.

### 2. Deterministic risk classifier — `classify_risk` in `approval/policy.py`

A pure function, the trust boundary in code:

```python
def classify_risk(tool_name: str, tool_input: dict) -> Risk: ...
```

`Risk` is an enum with three verdicts:

- **`SAFE`** — `can_use_tool` allows silently. No gate, no click. This is the
  common path for routine work.
- **`RISKY`** — `can_use_tool` opens the Slack gate. Matches the four categories
  agreed with the user, by patterns on tool name + arguments:
  1. **Destructive / irreversible** — `rm -rf`, `git push --force`,
     `kubectl delete`, SQL `DROP`/`DELETE`/`TRUNCATE`, dropping DBs, deleting
     resources.
  2. **Secrets / credentials** — reading or writing secrets, `.env`, private
     keys, Vault paths, kube secrets, token-bearing env.
  3. **External / outbound** — `curl`/`wget`/outbound POST, sending
     messages/emails, opening PRs, deploys — anything reaching outside the box
     or externally visible.
  4. **Prod / infra mutation** — `kubectl apply`/`rollout`, `terraform apply`,
     writes to prod namespaces/clusters, package installs.
- **`DENY`** — hard refuse in code, never prompts. Reserved for never-allow
  patterns (adopting slaude's OAuth-bootstrap hard-deny idea). Returns a
  `PermissionResultDeny` with an explanatory message. Empty at first beyond any
  such never-allow patterns we choose to encode; the enum exists so a
  "never, don't even ask" rule has a home separate from "ask a human".

Design notes:

- Classification is **allowlist-by-default for safety of the *human's
  attention*, not of the system**: unknown/unmatched tools default to `SAFE`
  (auto-allow), because the four categories are the agreed line and the goal is
  to stop blocking routine work. This is the deliberate difference from slaude's
  default-deny stance, and is what the user asked for ("if no unusual activity,
  no need for the gate at all"). Risky categories are matched explicitly.
- The matcher is data-driven where possible: a table of `(predicate ->
  Risk)` so categories are readable and unit-testable. Bash commands are matched
  on the parsed command string; Write/Edit on `file_path`; MCP tools on the
  tool id (e.g. `*_delete`, `*_apply`).
- **Never trust model paraphrase.** For Bash, classify the `command`, not the
  `description`. For any tool, classify structured args, not summary text.

### 3. Third gate button: `Approve` / `Always allow` / `Deny`

`gate.py`, `ports.py`, and the coordinator learn a third decision.

- **`ApprovalDecision`** gains a scope: the decision is one of
  `approved-once`, `approved-always`, or `denied` (plus the existing `by`
  Slack id / `"system"` sentinel). Concretely: add a field such as
  `scope: Literal["once", "always"]` alongside `approved: bool`, or fold into a
  single `verb`. Chosen representation is a planning detail; the behavior is
  three outcomes.
- **Action-id grammar** extends: `jean_appr:(approve|always|deny):<id>`.
  `ACTION_RE` and `handle_action` learn the `always` verb.
- **Coordinator `resolve`** persists the scope so the waiting worker (possibly a
  different one, via LISTEN/NOTIFY) knows whether the approval was once or
  always.
- **`can_use_tool`** on an `always` decision returns
  `PermissionResultAllow(updated_permissions=[PermissionUpdate(type="addRules",
  …, destination="session")])`. It honors the SDK's suggested pattern from
  `context` when present (e.g. `Bash(kubectl delete:*)`), else falls back to a
  tool-wide session rule. On `once` it returns a plain `PermissionResultAllow()`.
  On `deny` it returns `PermissionResultDeny(message=deny_reason(...),
  interrupt=False)` — unchanged.
- **Resolved-message rendering** (`_resolved_message`) gains the
  "Always-allowed for this session" headline, mirroring slaude's copy.

`context` (the third `can_use_tool` arg) is currently discarded (`del context`).
It must be read to extract `suggestions` for the `always` path.

### 4. Approver authorization — unchanged

`authz.py` `select_approvers` stays exactly as-is. A risky gate still selects
approvers by keyword-matching the summary against each approver's scope, with
the catchall / env / manager fallback ladder. The trust boundary (who may
approve) is untouched.

### 5. Removals — what "match slaude" unwinds

- **`agent_options.py`**: delete the `ExitPlanMode`-as-gate special case
  (the `if tool_name == "ExitPlanMode"` branch and its `seen_exit_plan`
  bookkeeping). The single remaining `can_use_tool` body becomes:
  classify → allow-silently / gate / hard-deny.
- **`session.py`**: delete the plan re-arm logic (the `reused and
  effective_mode == "plan"` block) and, if nothing else consumes it, the
  `default_permission_mode` constructor plumbing. A thread that manually chose
  `/mode plan` still opens in plan on a fresh connect via the options factory;
  it just no longer gets an automatic bypass-flip-then-re-arm cycle.
- **`policy.py` `summarize`**: the `ExitPlanMode` branch is no longer on the
  default path. Keep it harmless (a thread on manual plan mode can still hit it),
  but it is no longer the primary approval surface.

## Components and boundaries

| Unit | Responsibility | Depends on |
| --- | --- | --- |
| `classify_risk` (policy.py) | Pure `(tool, input) -> Risk`. The security decision. | nothing (pure) |
| `summarize` (policy.py) | Render a call as human-readable approval text. | `Risk`-independent |
| `ApprovalGate` (gate.py) | Post 3-button request, wait on coordinator, retire message. | `ApprovalCoordinator`, post/update callables, `authz` |
| `can_use_tool` (agent_options.py) | Wire classifier → gate → SDK permission result. | `classify_risk`, gate, SDK types |
| `ApprovalDecision` / coordinator (ports.py, db/*) | Carry & persist once/always/deny across workers. | Postgres / memory adapters |

Each is independently testable: `classify_risk` and `summarize` as pure
functions; the gate against a fake coordinator and fake post/update callables;
`can_use_tool` against a fake gate asserting the returned `PermissionResult`
shape for each verdict.

## Testing (TDD)

- `classify_risk`: a table of `(tool_name, input) -> expected Risk` covering
  each of the four risky categories, safe cases (workspace Write, `pytest`,
  local `git commit`, `kubectl get`), and any hard-deny patterns. Failing test
  first, then the classifier.
- Gate: `always` click produces a decision whose scope is `always`;
  authorization still rejects a non-approver clicking `always`; message retires
  with the always-copy.
- `can_use_tool`: `SAFE` → `PermissionResultAllow` with no prompt (gate not
  called); `RISKY` + `once` → allow; `RISKY` + `always` → allow with an
  `addRules` session permission; `RISKY` + deny → deny with `interrupt=False`;
  `DENY` → deny without ever calling the gate.
- Coordinator (memory + asyncpg): `resolve` round-trips the once/always scope.
- No live network; fakes at the ports; pristine output.

## Out of scope

- Changing the agent-driven `request_approval` tool (slaude's second gate) —
  it stays as jean's higher-level, persona-triggered checkpoint.
- Persisting `Always allow` rules **beyond the session** — session scope only,
  matching slaude. A durable per-thread allowlist is a possible future.
- Env-configurable auto-allow/deny lists (`SLAUDE_AUTO_ALLOW_TOOLS` analogue).
  The classifier is code, not config, for this iteration; an env override is a
  future addition if operators need it.

## Net behavior

- Routine mutation → **zero clicks** (classifier says `SAFE`).
- One of the four risky categories → **one click**, or **zero** after
  `Always allow` for that pattern this session.
- Every security decision is deterministic code; the LLM never classifies risk.
- The gate is an exception handler, not a blocker on ordinary work.

## Known limitations

1. **"Always allow" is session-scoped, not durable.** The `addRules` permission
   it writes lives on the in-process `ClaudeSDKClient`. A `turn_seq` mismatch
   that forces a rehydrate, or a later turn on the same thread landing on a
   different worker (a fresh `resume`d client, per the stateless-workers
   model), does not carry that rule forward -- the gate re-asks. This fails
   safe (it re-asks rather than silently allowing), but it means the "stops
   asking" promise only holds for as long as one worker keeps the same client
   cached. A durable per-thread allowlist (e.g. persisted in Postgres) is
   future work.
2. **The classifier protects the approver's attention, not against an
   adversarial agent.** `classify_risk` pattern-matches the literal command or
   tool id; it does not evaluate what the command does. Shell obfuscation --
   piping a script to `sh`/`bash -c`, `eval`, base64-decode-then-run, or any
   other indirection that hides the real action from the regex -- can land on
   `SAFE` even though the underlying action would have been `RISKY` if
   written plainly. This is an accepted limitation: the design's premise
   (CLAUDE.md's trust boundary) is that jean's own agent is Claude, not an
   adversary trying to evade the gate, so the classifier is scoped to keep a
   cooperative agent's routine work from needlessly pulling in a human, not
   to defeat deliberate evasion.

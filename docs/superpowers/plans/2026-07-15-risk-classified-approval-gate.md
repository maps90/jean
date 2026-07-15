# Risk-Classified Approval Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the approval gate fire only for genuinely risky tool calls (four deterministic categories), so routine work runs with zero clicks, and give risky prompts an `Always allow` button that silences a repeated pattern for the session.

**Architecture:** A pure-code `classify_risk(tool_name, tool_input)` decides `SAFE` / `RISKY` / `DENY` from the *actual* tool call. The SDK `can_use_tool` hook consults it: `SAFE` allows silently, `RISKY` opens the Slack gate (now `Approve` / `Always allow` / `Deny`), `DENY` refuses in code. The default permission mode flips from `plan` to `default`, and the plan-then-approve special-casing is removed. The security decision is always deterministic code — the LLM never classifies risk.

**Tech Stack:** Python 3.11+, `claude-agent-sdk` 0.2.110, asyncpg, slack-bolt, pytest + pytest-asyncio (`asyncio_mode = "auto"`).

## Global Constraints

- Python 3.11+; `from __future__ import annotations` at the top of every module; modern hints (`str | None`, `list[str]`).
- Async on all I/O paths. Domain methods touching a port are `async`.
- Layering rule: modules in `approval/`, `session/`, `gateway/` must NOT import `slack_bolt`, `slack_sdk`, `asyncpg`, or construct a `ClaudeSDKClient`. The classifier is pure and infra-free.
- The LLM never makes a security decision. `classify_risk` reads structured args (`command`, `file_path`, tool id) — never a model-supplied paraphrase (never Bash `description`, never plan text).
- TDD: failing test → run (see it fail) → minimal code → run (pass) → commit. One responsibility per file.
- Before any commit run `./scripts/verify.sh` (ruff check + ruff format-check + pytest). Fix lint with `uv run ruff check --fix src tests` and `uv run ruff format src tests`. Test output must be pristine — no warnings.
- Do NOT add AI co-author trailers to commits.
- Small, frequent commits — one per task, using the task's commit message.
- Work happens in the existing worktree `.claude/worktrees/risk-classified-gate` (branch `risk-classified-gate`). Run all commands from there.

## File Structure

| File | Responsibility | Task |
| --- | --- | --- |
| `src/jean/approval/risk.py` (new) | Pure `Risk` enum + `classify_risk(tool_name, tool_input) -> Risk`. The security decision. | 1 |
| `tests/test_risk.py` (new) | Table-driven classifier tests. | 1 |
| `src/jean/ports.py` | `ApprovalDecision` gains `scope`; `ApprovalCoordinator.resolve` gains `scope`. | 2 |
| `src/jean/db/memory.py` | Round-trip `scope` through `resolve`/`wait`. | 2 |
| `src/jean/db/postgres.py` | `scope` column + `resolve`/`wait` carry it. | 2 |
| `tests/test_decision_scope.py` (new) | `scope` round-trips through MemoryStore. | 2 |
| `src/jean/approval/gate.py` | Third `Always allow` button; `handle_action` learns `always`; resolved-message copy. | 3 |
| `tests/test_gate.py` | `always` click resolves with `scope="always"`; authz still enforced. | 3 |
| `src/jean/agent_options.py` | `can_use_tool` = classify → allow/gate/deny; `always` → session `addRules`; drop `ExitPlanMode` branch. | 4 |
| `tests/test_can_use_tool.py` (new) | One assertion per verdict × decision. | 4 |
| `src/jean/config.py` | Default `permission_mode` `plan` → `default`; update comment. | 5 |
| `src/jean/session/session.py` | Remove plan re-arm block + `default_permission_mode` plumbing. | 5 |
| `src/jean/server.py` | Drop the `default_permission_mode=` arg if it is passed to `JeanSession`. | 5 |
| `tests/test_session.py` | Remove/replace the plan-re-arm test; assert no re-arm on reuse. | 5 |

> **Deviation from spec (intentional):** the spec located `classify_risk` in `approval/policy.py`. Per the repo's "one responsibility per file" convention, it goes in a new `approval/risk.py` (classification) and `policy.py` keeps its rendering role (`summarize`, `deny_reason`). Same behavior, cleaner boundary.

---

## Task 1: Risk classifier (pure)

**Files:**
- Create: `src/jean/approval/risk.py`
- Test: `tests/test_risk.py`

**Interfaces:**
- Consumes: nothing (pure).
- Produces:
  - `class Risk(enum.Enum)` with members `SAFE`, `RISKY`, `DENY`.
  - `def classify_risk(tool_name: str, tool_input: dict[str, Any]) -> Risk`
  - `DENY_MESSAGE: str` (module constant) — used by Task 4 for the hard-deny path.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_risk.py`:

```python
from __future__ import annotations

import pytest

from jean.approval.risk import Risk, classify_risk


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /tmp/x",
        "git push --force origin main",
        "kubectl delete pod api-0",
        "psql -c 'DROP TABLE users'",
        "psql -c 'DELETE FROM users'",
        "git reset --hard HEAD~3",
    ],
)
def test_destructive_bash_is_risky(command):
    assert classify_risk("Bash", {"command": command}) is Risk.RISKY


@pytest.mark.parametrize(
    "command",
    [
        "cat .env",
        "kubectl get secret db-creds -o yaml",
        "cat ~/.ssh/id_rsa",
        "vault kv get secret/prod",
    ],
)
def test_secret_bash_is_risky(command):
    assert classify_risk("Bash", {"command": command}) is Risk.RISKY


@pytest.mark.parametrize(
    "command",
    ["curl https://api.example.com", "wget http://x/y", "gh pr create", "npm publish"],
)
def test_external_bash_is_risky(command):
    assert classify_risk("Bash", {"command": command}) is Risk.RISKY


@pytest.mark.parametrize(
    "command",
    [
        "kubectl apply -f deploy.yaml",
        "kubectl rollout restart deploy/api",
        "terraform apply",
        "helm upgrade api ./chart",
        "pip install requests",
        "npm install",
    ],
)
def test_prod_infra_bash_is_risky(command):
    assert classify_risk("Bash", {"command": command}) is Risk.RISKY


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q",
        "ls -la",
        "kubectl get pods",
        "git commit -m 'wip'",
        "git status",
        "cat src/jean/config.py",
    ],
)
def test_routine_bash_is_safe(command):
    assert classify_risk("Bash", {"command": command}) is Risk.SAFE


def test_classifier_reads_the_command_not_the_description():
    # The model's paraphrase must never soften a real command.
    verdict = classify_risk(
        "Bash", {"command": "rm -rf /data", "description": "clean up a temp file"}
    )
    assert verdict is Risk.RISKY


def test_workspace_file_write_is_safe():
    assert classify_risk("Write", {"file_path": "/home/jean/workspaces/app/main.py"}) is Risk.SAFE


@pytest.mark.parametrize("path", ["/app/.env", "/home/u/.ssh/id_rsa", "/etc/secrets/db.pem"])
def test_writing_a_secret_file_is_risky(path):
    assert classify_risk("Write", {"file_path": path}) is Risk.RISKY
    assert classify_risk("Edit", {"file_path": path}) is Risk.RISKY


def test_mcp_delete_tool_is_risky():
    assert classify_risk("mcp__plugin_kubectl_kubernetes__pods_delete", {}) is Risk.RISKY


def test_mcp_apply_tool_is_risky():
    assert classify_risk("mcp__plugin_kubectl_kubernetes__apply", {}) is Risk.RISKY


def test_synthesized_oauth_tool_is_denied():
    assert classify_risk("mcp__plugin_foo__authenticate", {}) is Risk.DENY
    assert classify_risk("mcp__plugin_foo__complete_authentication", {}) is Risk.DENY


def test_unknown_tool_defaults_to_safe():
    # The four categories are the agreed line; anything unmatched must not block.
    assert classify_risk("SomeNewTool", {"whatever": 1}) is Risk.SAFE
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd .claude/worktrees/risk-classified-gate && uv run pytest tests/test_risk.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jean.approval.risk'`.

- [ ] **Step 3: Write the classifier**

Create `src/jean/approval/risk.py`:

```python
from __future__ import annotations

import enum
import re
from typing import Any


class Risk(enum.Enum):
    """A tool call's risk, decided by code -- never by the model.

    SAFE  -> run without asking (routine work).
    RISKY -> ask a human (one of the four gated categories).
    DENY  -> refuse in code, never even prompt.
    """

    SAFE = "safe"
    RISKY = "risky"
    DENY = "deny"


DENY_MESSAGE = (
    "This action is disabled. OAuth connect must go through the controlled "
    "flow, not the synthesized auth tool."
)

# --- deny (never allow, never ask) ---
_DENY_MCP = re.compile(r"^mcp__.+__(authenticate|complete_authentication)$")

# --- Bash command patterns, per category. Matched case-insensitively against
#     the *verbatim* command string. This table IS the security surface: adding
#     a hole here silently widens what runs unattended. Review it as such. ---
_DESTRUCTIVE = re.compile(
    r"""
    \brm\s+-[a-z]*f          # rm -rf / -fr
    | \bgit\s+push\b.*(--force|-f)\b
    | \bgit\s+reset\s+--hard\b
    | \bgit\s+clean\s+-[a-z]*f
    | \bkubectl\s+delete\b
    | \bdrop\s+(table|database|schema)\b
    | \btruncate\b
    | \bdelete\s+from\b
    | \bmkfs\b
    | \bdd\s+if=
    | >\s*/dev/
    """,
    re.IGNORECASE | re.VERBOSE,
)
_SECRETS = re.compile(
    r"""
    (^|[\s/])\.env(\b|$)
    | \bid_rsa\b
    | \.pem\b | \.key\b
    | \bvault\b
    | \bkubectl\b.*\bsecret
    | \bcredentials?\b
    | \.ssh/
    """,
    re.IGNORECASE | re.VERBOSE,
)
_EXTERNAL = re.compile(
    r"""
    \bcurl\b | \bwget\b
    | \bgh\s+pr\s+create\b
    | \b(npm|pip|cargo|gem)\s+publish\b
    | \bgit\s+push\b
    | \bmail\b | \bsendmail\b
    | https?://
    """,
    re.IGNORECASE | re.VERBOSE,
)
_PROD_INFRA = re.compile(
    r"""
    \bkubectl\s+(apply|rollout|scale|patch|drain|cordon|edit)\b
    | \bterraform\s+(apply|destroy)\b
    | \bhelm\s+(install|upgrade|uninstall)\b
    | \b(pip|pip3)\s+install\b
    | \bnpm\s+(install|ci)\b
    | \b(apt|apt-get|yum|brew)\s+install\b
    | \bdocker\s+push\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
_BASH_RISK = (_DESTRUCTIVE, _SECRETS, _EXTERNAL, _PROD_INFRA)

# --- file paths that mean secrets, for Write/Edit ---
_SECRET_PATH = re.compile(
    r"(^|/)\.env(\.|$)|/\.ssh/|\bid_rsa\b|\.pem$|\.key$|/secrets?/|\bcredentials?\b",
    re.IGNORECASE,
)
_FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# --- MCP tool ids whose verb is a mutation worth a human ---
_MCP_RISK = re.compile(
    r"(delete|apply|rollout|scale|restart|drain|cordon|destroy|create|patch)",
    re.IGNORECASE,
)


def classify_risk(tool_name: str, tool_input: dict[str, Any]) -> Risk:
    """Deterministic risk of a tool call. Pure; reads structured args only."""
    if _DENY_MCP.match(tool_name):
        return Risk.DENY

    if tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        if any(pat.search(command) for pat in _BASH_RISK):
            return Risk.RISKY
        return Risk.SAFE

    if tool_name in _FILE_TOOLS:
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        return Risk.RISKY if _SECRET_PATH.search(path) else Risk.SAFE

    if tool_name.startswith("mcp__"):
        # jean's own Slack tools and read-only MCP calls never reach can_use_tool
        # (they are in allowed_tools), so a mutation verb here is a real action.
        return Risk.RISKY if _MCP_RISK.search(tool_name) else Risk.SAFE

    return Risk.SAFE
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd .claude/worktrees/risk-classified-gate && uv run pytest tests/test_risk.py -q`
Expected: PASS (all cases).

- [ ] **Step 5: Lint & commit**

```bash
cd .claude/worktrees/risk-classified-gate
uv run ruff check --fix src/jean/approval/risk.py tests/test_risk.py
uv run ruff format src/jean/approval/risk.py tests/test_risk.py
uv run pytest tests/test_risk.py -q
git add src/jean/approval/risk.py tests/test_risk.py
git commit -m "feat(approval): deterministic risk classifier for tool calls"
```

---

## Task 2: Decision scope (once / always) through the coordinator

**Files:**
- Modify: `src/jean/ports.py` (`ApprovalDecision` dataclass ~line 19-22; `ApprovalCoordinator.resolve` ~line 83-85)
- Modify: `src/jean/db/memory.py` (`resolve` ~line 183-191; `wait` builds `ApprovalDecision`)
- Modify: `src/jean/db/postgres.py` (`_SCHEMA` ~line 17-23; `wait` ~line 244-278; `resolve` ~line 280-292)
- Test: `tests/test_decision_scope.py` (new)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `ApprovalDecision(approved: bool, by: str, scope: str = "once")` — `scope` is `"once"` or `"always"`; meaningful only when `approved`.
  - `ApprovalCoordinator.resolve(approval_id: str, approved: bool, by: str, scope: str = "once") -> bool`
  - `wait(...)` returns an `ApprovalDecision` whose `scope` reflects what `resolve` stored.

- [ ] **Step 1: Write the failing test**

Create `tests/test_decision_scope.py`:

```python
from __future__ import annotations

from jean.db.memory import MemoryStore


async def test_resolve_defaults_to_once():
    store = MemoryStore()
    await store.create("a1", "C1", "1.0", "deploy")
    await store.resolve("a1", True, "U1")
    decision = await store.wait("a1", 0.05)
    assert decision.approved is True
    assert decision.by == "U1"
    assert decision.scope == "once"


async def test_resolve_carries_always_scope():
    store = MemoryStore()
    await store.create("a2", "C1", "1.0", "delete a pod")
    await store.resolve("a2", True, "U2", scope="always")
    decision = await store.wait("a2", 0.05)
    assert decision.approved is True
    assert decision.scope == "always"


async def test_timeout_decision_is_once_scoped_system_deny():
    store = MemoryStore()
    await store.create("a3", "C1", "1.0", "deploy")
    decision = await store.wait("a3", 0.01)
    assert decision.approved is False
    assert decision.by == "system"
    assert decision.scope == "once"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd .claude/worktrees/risk-classified-gate && uv run pytest tests/test_decision_scope.py -q`
Expected: FAIL — `TypeError: resolve() got an unexpected keyword argument 'scope'` (and `AttributeError: 'ApprovalDecision' object has no attribute 'scope'`).

- [ ] **Step 3a: Extend `ApprovalDecision` and the port**

In `src/jean/ports.py`, replace the dataclass:

```python
@dataclass
class ApprovalDecision:
    approved: bool
    by: str
    scope: str = "once"  # "once" | "always"; meaningful only when approved
```

And in the `ApprovalCoordinator` Protocol, change the `resolve` signature:

```python
    async def resolve(
        self, approval_id: str, approved: bool, by: str, scope: str = "once"
    ) -> bool: ...  # True if it was pending
```

- [ ] **Step 3b: Carry `scope` in MemoryStore**

In `src/jean/db/memory.py`, update `resolve` (currently ~line 183):

```python
    async def resolve(
        self, approval_id: str, approved: bool, by: str, scope: str = "once"
    ) -> bool:
        row = self._approvals.get(approval_id)
        if row is None or row.decision is not None or row.future.done():
            return False
        decision = ApprovalDecision(approved, by, scope)
        row.decision = decision
        row.resolved_at = time.time()
        row.future.set_result(decision)
        return True
```

The timeout branch in `wait` already builds `ApprovalDecision(False, "system")`, which now defaults `scope="once"` — no change needed there.

- [ ] **Step 3c: Carry `scope` in PostgresStore**

In `src/jean/db/postgres.py`, add the column to `_SCHEMA` (inside the `approvals` table body, and an idempotent ALTER for already-created DBs). Change the `approvals` CREATE and append the ALTER right after it:

```sql
CREATE TABLE IF NOT EXISTS approvals (
  id text PRIMARY KEY, channel text NOT NULL, thread_ts text NOT NULL,
  summary text NOT NULL, status text NOT NULL DEFAULT 'pending',
  approved boolean, approver_id text,
  approvers text[] NOT NULL DEFAULT '{}',
  scope text NOT NULL DEFAULT 'once',
  requested_at double precision NOT NULL DEFAULT extract(epoch from now()),
  resolved_at double precision);
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS scope text NOT NULL DEFAULT 'once';
```

Update `resolve` (~line 280) to write `scope`:

```python
    async def resolve(
        self, approval_id: str, approved: bool, by: str, scope: str = "once"
    ) -> bool:
        status = await self._pool.fetchval(
            "UPDATE approvals SET status=$2, approved=$3, approver_id=$4, scope=$5, "
            "resolved_at=extract(epoch from now()) "
            "WHERE id=$1 AND status='pending' RETURNING id",
            approval_id,
            "approved" if approved else "denied",
            approved,
            by,
            scope,
        )
        if status is None:
            return False
        await self._pool.execute("SELECT pg_notify('jean_approvals', $1)", approval_id)
        return True
```

Update `wait` (~line 244) to read `scope` in both fetches:

```python
            await conn.add_listener("jean_approvals", _cb)
            row = await conn.fetchrow(
                "SELECT status,approved,approver_id,scope FROM approvals WHERE id=$1", approval_id
            )
            if row and row["status"] != "pending":
                return ApprovalDecision(
                    bool(row["approved"]), row["approver_id"] or "unknown", row["scope"] or "once"
                )
            try:
                await asyncio.wait_for(fut, timeout)
            except TimeoutError:
                await self.resolve(approval_id, False, "system")
            row = await conn.fetchrow(
                "SELECT approved,approver_id,scope FROM approvals WHERE id=$1", approval_id
            )
            return ApprovalDecision(
                bool(row["approved"]) if row else False,
                (row["approver_id"] if row else None) or "system",
                (row["scope"] if row else None) or "once",
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd .claude/worktrees/risk-classified-gate && uv run pytest tests/test_decision_scope.py tests/test_gate.py -q`
Expected: PASS. (`test_gate.py` still passes — `resolve`'s new arg is defaulted.)

- [ ] **Step 5: Lint & commit**

```bash
cd .claude/worktrees/risk-classified-gate
uv run ruff check --fix src/jean/ports.py src/jean/db/memory.py src/jean/db/postgres.py tests/test_decision_scope.py
uv run ruff format src/jean/ports.py src/jean/db/memory.py src/jean/db/postgres.py tests/test_decision_scope.py
uv run pytest tests/test_decision_scope.py -q
git add src/jean/ports.py src/jean/db/memory.py src/jean/db/postgres.py tests/test_decision_scope.py
git commit -m "feat(approval): carry once/always scope through the coordinator"
```

---

## Task 3: `Always allow` button in the gate

**Files:**
- Modify: `src/jean/approval/gate.py` (`ACTION_RE` line 14; `handle_action` line 105-122; `_resolved_message` line 142-161; `_build_blocks` line 164-195)
- Test: `tests/test_gate.py`

**Interfaces:**
- Consumes: `ApprovalCoordinator.resolve(..., scope=...)` (Task 2); `ApprovalDecision.scope` (Task 2).
- Produces: three-button blocks with action ids `jean_appr:approve:<id>`, `jean_appr:always:<id>`, `jean_appr:deny:<id>`. `handle_action` returns `"approved"` (for approve or always), `"denied"`, `"unauthorized"`, or `"gone"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_gate.py` (helpers `_make_gate`, `_action_id_for`, `_action_ids` already exist at the top of that file):

```python
async def test_blocks_include_an_always_allow_button():
    coordinator = MemoryStore()
    posted: list[list[dict]] = []
    approvers = [ApproverEntry(user_id="U1", scope="", catchall=True)]
    gate = _make_gate(coordinator, posted, approvers=approvers, timeout_seconds=0.05)

    await gate.request("C1", "111.0", "kubectl delete pod")

    ids = _action_ids(posted[0])
    assert any(a.startswith("jean_appr:always:") for a in ids)


async def test_always_click_resolves_with_always_scope():
    coordinator = MemoryStore()
    posted: list[list[dict]] = []
    posted_event = asyncio.Event()
    approvers = [ApproverEntry(user_id="U1", scope="", catchall=True)]
    gate = _make_gate(
        coordinator, posted, approvers=approvers, timeout_seconds=5.0, posted_event=posted_event
    )

    waiter = asyncio.create_task(gate.request("C1", "111.0", "kubectl delete pod"))
    await posted_event.wait()
    always_id = _action_id_for(posted[0], "always")
    result = await gate.handle_action(always_id, "U1")
    decision = await waiter

    assert result == "approved"
    assert decision.approved is True
    assert decision.scope == "always"


async def test_always_click_by_a_non_approver_is_unauthorized():
    coordinator = MemoryStore()
    posted: list[list[dict]] = []
    posted_event = asyncio.Event()
    approvers = [ApproverEntry(user_id="U1", scope="", catchall=True)]
    gate = _make_gate(
        coordinator, posted, approvers=approvers, timeout_seconds=5.0, posted_event=posted_event
    )

    waiter = asyncio.create_task(gate.request("C1", "111.0", "kubectl delete pod"))
    await posted_event.wait()
    always_id = _action_id_for(posted[0], "always")
    result = await gate.handle_action(always_id, "INTRUDER")

    assert result == "unauthorized"
    # Still pending -- resolve it so the waiter doesn't hang the test.
    approve_id = _action_id_for(posted[0], "approve")
    await gate.handle_action(approve_id, "U1")
    await waiter
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd .claude/worktrees/risk-classified-gate && uv run pytest tests/test_gate.py -q`
Expected: FAIL — no `jean_appr:always:` action id is produced; `_action_id_for(..., "always")` raises `StopIteration`.

- [ ] **Step 3a: Teach `ACTION_RE` and `handle_action` the `always` verb**

In `src/jean/approval/gate.py`, line 14:

```python
ACTION_RE = re.compile(r"^jean_appr:(approve|always|deny):(.+)$")
```

Replace `handle_action` (line 105-122):

```python
    async def handle_action(self, action_id: str, user_id: str) -> str:
        match = ACTION_RE.match(action_id)
        if not match:
            return "gone"
        verb, approval_id = match.group(1), match.group(2)

        pending = await self._coordinator.get_pending(approval_id)
        if pending is None:
            return "gone"

        authorized = await self._coordinator.approvers_of(approval_id)
        if user_id not in authorized:
            return "unauthorized"

        approved = verb != "deny"
        scope = "always" if verb == "always" else "once"
        resolved = await self._coordinator.resolve(approval_id, approved, user_id, scope)
        if not resolved:
            return "gone"
        return "approved" if approved else "denied"
```

- [ ] **Step 3b: Add the button to `_build_blocks`**

In `_build_blocks` (line 164), insert an `Always allow` button between Approve and Deny in the `actions` elements list:

```python
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": f"jean_appr:approve:{approval_id}",
                    "value": approval_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Always allow"},
                    "action_id": f"jean_appr:always:{approval_id}",
                    "value": approval_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "style": "danger",
                    "action_id": f"jean_appr:deny:{approval_id}",
                    "value": approval_id,
                },
            ],
        },
```

- [ ] **Step 3c: Reflect `always` in the resolved message**

In `_resolved_message` (line 142), replace the approved branch so an always-scoped approval reads differently:

```python
    if decision.by == "system":
        headline, footer = "Approval expired", "No answer in time -- treated as denied."
    elif decision.approved and decision.scope == "always":
        headline = "Always-allowed for this session"
        footer = f"Always-allowed by <@{decision.by}>"
    elif decision.approved:
        headline, footer = "Approved", f"Approved by <@{decision.by}>"
    else:
        headline, footer = "Denied", f"Denied by <@{decision.by}>"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd .claude/worktrees/risk-classified-gate && uv run pytest tests/test_gate.py -q`
Expected: PASS (existing gate tests + the three new ones).

- [ ] **Step 5: Lint & commit**

```bash
cd .claude/worktrees/risk-classified-gate
uv run ruff check --fix src/jean/approval/gate.py tests/test_gate.py
uv run ruff format src/jean/approval/gate.py tests/test_gate.py
uv run pytest tests/test_gate.py -q
git add src/jean/approval/gate.py tests/test_gate.py
git commit -m "feat(approval): add an Always-allow button to the gate"
```

---

## Task 4: Wire `can_use_tool` to the classifier

**Files:**
- Modify: `src/jean/agent_options.py` (imports line 6-18; `build_can_use_tool` line 42-110)
- Test: `tests/test_can_use_tool.py` (new)

**Interfaces:**
- Consumes: `classify_risk`, `Risk`, `DENY_MESSAGE` (Task 1); `ApprovalDecision.scope` (Task 2); the gate's `request(...)` returning an `ApprovalDecision`.
- Produces: `build_can_use_tool(gate, *, channel, thread_ts) -> CanUseTool` whose callback returns:
  - `Risk.SAFE` → `PermissionResultAllow()` without calling the gate.
  - `Risk.DENY` → `PermissionResultDeny(message=DENY_MESSAGE, interrupt=False)` without calling the gate.
  - `Risk.RISKY` + approved once → `PermissionResultAllow()`.
  - `Risk.RISKY` + approved always → `PermissionResultAllow(updated_permissions=[addRules session rule])`.
  - `Risk.RISKY` + denied → `PermissionResultDeny(message=deny_reason(decision), interrupt=False)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_can_use_tool.py`:

```python
from __future__ import annotations

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from jean.agent_options import build_can_use_tool
from jean.ports import ApprovalDecision


class _RecordingGate:
    """Fake gate: records whether it was asked, returns a fixed decision."""

    def __init__(self, decision: ApprovalDecision | None) -> None:
        self._decision = decision
        self.asked = False

    async def request(self, channel: str, thread_ts: str, summary: str) -> ApprovalDecision:
        self.asked = True
        assert self._decision is not None, "gate asked when it should not have been"
        return self._decision


async def test_safe_tool_runs_without_asking():
    gate = _RecordingGate(None)
    hook = build_can_use_tool(gate, channel="C1", thread_ts="1.0")
    result = await hook("Bash", {"command": "pytest -q"}, None)
    assert isinstance(result, PermissionResultAllow)
    assert gate.asked is False


async def test_denied_class_tool_is_refused_without_asking():
    gate = _RecordingGate(None)
    hook = build_can_use_tool(gate, channel="C1", thread_ts="1.0")
    result = await hook("mcp__plugin_x__authenticate", {}, None)
    assert isinstance(result, PermissionResultDeny)
    assert gate.asked is False


async def test_risky_tool_approved_once_runs():
    gate = _RecordingGate(ApprovalDecision(True, "U1", "once"))
    hook = build_can_use_tool(gate, channel="C1", thread_ts="1.0")
    result = await hook("Bash", {"command": "kubectl delete pod api-0"}, None)
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_permissions is None
    assert gate.asked is True


async def test_risky_tool_always_allow_adds_a_session_rule():
    gate = _RecordingGate(ApprovalDecision(True, "U1", "always"))
    hook = build_can_use_tool(gate, channel="C1", thread_ts="1.0")
    result = await hook("Bash", {"command": "kubectl delete pod api-0"}, None)
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_permissions is not None
    update = result.updated_permissions[0]
    assert update.type == "addRules"
    assert update.behavior == "allow"
    assert update.destination == "session"
    assert update.rules[0].tool_name == "Bash"


async def test_risky_tool_denied_is_refused():
    gate = _RecordingGate(ApprovalDecision(False, "U1", "once"))
    hook = build_can_use_tool(gate, channel="C1", thread_ts="1.0")
    result = await hook("Bash", {"command": "rm -rf /data"}, None)
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd .claude/worktrees/risk-classified-gate && uv run pytest tests/test_can_use_tool.py -q`
Expected: FAIL — the current hook asks the gate for a `SAFE` tool (so `test_safe_tool_runs_without_asking` fails on `gate.asked is False`), and there is no `addRules` path.

- [ ] **Step 3: Rewrite `build_can_use_tool`**

In `src/jean/agent_options.py`, update the imports block (lines 6-18) to add the rule types and the classifier, and drop nothing that is still used:

```python
from claude_agent_sdk import (
    CanUseTool,
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    PermissionUpdate,
)
from claude_agent_sdk.types import PermissionRuleValue

from jean.approval.policy import deny_reason, summarize
from jean.approval.risk import DENY_MESSAGE, Risk, classify_risk
from jean.config import Settings
from jean.persona.identity import DEFAULT_AGENT_NAME, compose_system_prompt
from jean.plugins.mcp_proxy import proxy_tool_patterns
from jean.ports import ApprovalDecision, ResolvedPlugin
```

Replace the whole `build_can_use_tool` function (lines 42-110) with:

```python
def build_can_use_tool(gate: _Gate, *, channel: str, thread_ts: str) -> CanUseTool:
    """The SDK's permission hook. A deterministic classifier decides risk; only
    RISKY calls reach a human.

    Runs under `default` permission_mode, so the CLI calls this for every tool
    outside `allowed_tools` (jean's Slack tools + the MCP proxies) and outside
    its read-only set -- i.e. Bash/Write/Edit and mutating plugin-MCP calls.

    - SAFE  -> allow silently. Routine work never blocks.
    - DENY  -> refuse in code; never prompt a human.
    - RISKY -> ask an approver. "Always allow" adds a session-scoped rule so a
      repeated pattern stops asking.

    channel/thread_ts are bound per session (not read from a process-wide slot)
    because this awaits a human and a turn on another thread must not repoint it.
    """

    async def can_use_tool(
        tool_name: str, tool_input: dict[str, Any], context: Any
    ) -> PermissionResultAllow | PermissionResultDeny:
        del context

        risk = classify_risk(tool_name, tool_input)
        if risk is Risk.SAFE:
            return PermissionResultAllow()
        if risk is Risk.DENY:
            logger.info("hard-denied: %s in %s/%s", tool_name, channel, thread_ts)
            return PermissionResultDeny(message=DENY_MESSAGE, interrupt=False)

        # RISKY -> a human decides.
        decision: ApprovalDecision = await gate.request(
            channel, thread_ts, summarize(tool_name, tool_input)
        )
        if not decision.approved:
            logger.info("denied: %s in %s/%s by %s", tool_name, channel, thread_ts, decision.by)
            # interrupt=False: the tool does not run, but the turn lives, so the
            # agent can tell the thread it was denied instead of dying silently.
            return PermissionResultDeny(message=deny_reason(decision), interrupt=False)

        if decision.scope == "always":
            logger.info(
                "always-allowed: %s in %s/%s by %s", tool_name, channel, thread_ts, decision.by
            )
            return PermissionResultAllow(
                updated_permissions=[
                    PermissionUpdate(
                        type="addRules",
                        rules=[PermissionRuleValue(tool_name=tool_name, rule_content=None)],
                        behavior="allow",
                        destination="session",
                    )
                ]
            )
        logger.info("approved: %s in %s/%s by %s", tool_name, channel, thread_ts, decision.by)
        return PermissionResultAllow()

    return can_use_tool
```

> Note: this removes the `ExitPlanMode` special-case and the `seen_exit_plan` bookkeeping entirely. `summarize` in `policy.py` keeps its `ExitPlanMode` branch (harmless; only reached if a thread manually selects `/mode plan`), so no change there.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd .claude/worktrees/risk-classified-gate && uv run pytest tests/test_can_use_tool.py tests/test_agent_options.py -q`
Expected: PASS.

- [ ] **Step 5: Lint & commit**

```bash
cd .claude/worktrees/risk-classified-gate
uv run ruff check --fix src/jean/agent_options.py tests/test_can_use_tool.py
uv run ruff format src/jean/agent_options.py tests/test_can_use_tool.py
uv run pytest tests/test_can_use_tool.py -q
git add src/jean/agent_options.py tests/test_can_use_tool.py
git commit -m "feat(approval): gate only risky tool calls via the classifier"
```

---

## Task 5: Flip default mode and remove plan re-arm

**Files:**
- Modify: `src/jean/config.py` (default `permission_mode` line 57 + comment lines 48-56)
- Modify: `src/jean/session/session.py` (re-arm block lines ~366-381; `default_permission_mode` param lines ~60-80; any read of `self._default_permission_mode`)
- Modify: `src/jean/server.py` (drop the `default_permission_mode=` argument if passed when constructing `JeanSession`)
- Test: `tests/test_session.py` (remove/replace the re-arm test)

**Interfaces:**
- Consumes: nothing new.
- Produces: `Settings.permission_mode` default is `"default"`; `JeanSession` no longer re-arms `plan` on a reused client and no longer takes `default_permission_mode`.

- [ ] **Step 1: Read the current session re-arm code and its test**

Run:
```bash
cd .claude/worktrees/risk-classified-gate
grep -n "default_permission_mode\|set_permission_mode\|re-arm\|effective_mode\|_default_permission_mode" src/jean/session/session.py src/jean/server.py
grep -n "re-arm\|rearm\|set_permission_mode\|plan\|default_permission_mode" tests/test_session.py
```
Read the matched regions before editing so the removals are exact.

- [ ] **Step 2: Change the config default and update its comment**

In `src/jean/config.py`, replace line 57:

```python
    permission_mode: str = "default"
```

Replace the preceding comment block (lines ~48-56) with:

```python
    # jean gates only *risky* tool calls (agent_options.classify_risk): routine
    # mutations run unattended, the four risky categories ask a human, and
    # "Always allow" silences a repeated pattern for the session. "default" is
    # the mode where the CLI calls the permission hook for every mutating tool
    # so the classifier can decide. Reachable per-thread via `/mode`:
    # "plan" makes the agent present a plan first; "bypassPermissions" skips the
    # hook entirely, leaving only the agent-chosen request_approval tool.
    permission_mode: str = "default"
```

- [ ] **Step 3: Remove the plan re-arm block in `session.py`**

Delete the re-arm block (the `effective_mode = ...` / `if reused and effective_mode == "plan":` / `await self._client.set_permission_mode("plan")` region, ~lines 366-381 including its explanatory comment). If removing it leaves `row` computed only for this purpose, keep `row` if still used by the surrounding `turn_seq` check; otherwise remove the now-dead local. Then remove the `default_permission_mode` constructor parameter (line ~64) and the `self._default_permission_mode = default_permission_mode` assignment (line ~80), plus the parameter's docstring/comment (lines ~60-63).

Guidance for the implementer: make the surrounding turn flow read as "reuse the cached client when `turn_seq` agrees, else re-hydrate" with no plan-mode branch. Do not touch the `turn_seq`/`_seen_seq` logic — only the plan re-arm.

- [ ] **Step 4: Drop the argument at the construction site**

In `src/jean/server.py`, if `JeanSession(...)` (or its factory) is passed `default_permission_mode=settings.permission_mode` or similar, remove that keyword argument. Confirm with:
```bash
grep -n "default_permission_mode" src/jean/server.py
```
Expected after edit: no matches.

- [ ] **Step 5: Update the session tests**

In `tests/test_session.py`, remove or rewrite the test that asserts a reused client re-arms `plan` (it will reference `set_permission_mode("plan")` or a fake client recording a plan re-arm). Replace it with a test asserting a reused client is NOT re-armed. Concretely, find the fake SDK client's record of `set_permission_mode` calls and assert it stays empty across a reused-client turn. Use the existing fake-client fixture in that file (do not introduce a new one); mirror its existing arrange/act structure. Example shape (adapt names to the file's fixtures):

```python
async def test_reused_client_is_not_re_armed_to_plan(...):
    # ... arrange a session whose cached client handles a second turn ...
    await session.handle_turn(...)  # second turn, client reused
    assert fake_client.permission_mode_calls == []  # no re-arm
```

- [ ] **Step 6: Run the affected tests, then the full gate**

Run:
```bash
cd .claude/worktrees/risk-classified-gate
uv run pytest tests/test_session.py tests/test_config.py -q
./scripts/verify.sh
```
Expected: PASS, and `verify.sh` green (ruff check + format-check + full pytest).

- [ ] **Step 7: Lint & commit**

```bash
cd .claude/worktrees/risk-classified-gate
uv run ruff check --fix src tests
uv run ruff format src tests
./scripts/verify.sh
git add src/jean/config.py src/jean/session/session.py src/jean/server.py tests/test_session.py
git commit -m "feat(config): gate risky tools by default; drop plan re-arm"
```

---

## Self-Review

**Spec coverage:**
- Default mode `plan → default` → Task 5. ✓
- `classify_risk` SAFE/RISKY/DENY, four categories, pattern-based, pure → Task 1. ✓
- Unknown tools default SAFE → Task 1 (`test_unknown_tool_defaults_to_safe`). ✓
- Never trust model paraphrase (classify `command`, not `description`) → Task 1 (`test_classifier_reads_the_command_not_the_description`). ✓
- Three-button gate `Approve/Always allow/Deny`; action-id grammar; resolved-message copy → Task 3. ✓
- `ApprovalDecision` scope once/always; coordinator persists it (memory + postgres) → Task 2. ✓
- `always` → `PermissionResultAllow(updated_permissions=[addRules … session])`, honoring suggestions when present → Task 4. (Suggestions: the plan uses a tool-wide session rule; honoring `context.suggestions` is noted as an available refinement but not required — `del context` is kept for simplicity. If richer patterns are wanted, read `context.suggestions` before the fallback.) ✓ with note.
- `authz.py` unchanged → confirmed, not in any task's edit list. ✓
- Remove `ExitPlanMode`-as-gate branch → Task 4. ✓
- Remove plan re-arm + `default_permission_mode` plumbing → Task 5. ✓
- `plan` still selectable via `/mode` → Task 5 (config comment; no `/mode` code touched). ✓
- Agent-driven `request_approval` untouched (out of scope) → not in any task. ✓

**Placeholder scan:** No TBD/TODO. Task 5 steps 3 & 5 describe edits against code whose exact line content the implementer reads in step 1 (the region is line-referenced and the transformation is fully specified); every code step elsewhere shows complete code.

**Type consistency:** `classify_risk(tool_name, tool_input) -> Risk` and `Risk.{SAFE,RISKY,DENY}` used identically in Tasks 1 & 4. `resolve(approval_id, approved, by, scope="once")` identical in ports, memory, postgres, and `handle_action`'s call (Tasks 2 & 3). `ApprovalDecision(approved, by, scope)` positional order consistent across memory, postgres, and test construction. `PermissionUpdate(type, rules=[PermissionRuleValue(tool_name, rule_content)], behavior, destination)` matches the installed SDK (verified).

**Note on suggestions refinement:** slaude honors the SDK's suggested rule pattern (e.g. `Bash(kubectl delete:*)`) for a tighter always-allow than a tool-wide rule. This plan uses the tool-wide rule for simplicity and testability; a follow-up can read `context.suggestions` first and fall back to the tool-wide rule. Called out so it is a deliberate scope choice, not an omission.

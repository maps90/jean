# Remote MCP env expansion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand `${VAR}` references in a remote (http/sse) MCP server's config, so Portico can be registered in `mcp.json` with its bearer token supplied from the environment — which takes Portico off the `Bash`/`curl` path and therefore out of the approval gate entirely.

**Architecture:** Env-reference expansion currently lives inside `plugins/mcp_stdio.py` (`expand()`, applied only to a stdio server's `env` block). Lift it into its own module, `plugins/env_refs.py`, and add a **strict** variant for remote configs that raises on an unset variable instead of substituting `""`. Then apply that strict variant in `remote_servers()`. Two collaborators, one responsibility each; `remote_servers()` stays a pure function over a dict, so all of this is unit-testable with no network, no DB, and no CLI.

**Tech Stack:** Python 3.11+, pytest (`asyncio_mode = "auto"`), ruff. No new dependencies.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-13-remote-mcp-env-expansion-design.md`.
- Work happens in the worktree `../jean-remote-mcp-env`, branch `remote-mcp-env`. Never in the primary checkout.
- `from __future__ import annotations` at the top of every module; modern type hints (`str | None`, `list[str]`).
- Domain modules must not import `slack_bolt`, `slack_sdk`, or `asyncpg`. Nothing here needs to.
- Run `./scripts/verify.sh` (ruff check + ruff format-check + pytest) before every commit. Test output must be pristine — no stray warnings.
- **Do NOT add AI co-author trailers to commits in this repo.**
- Sequential execution only — one task, start to finish, then the next.
- **Lenient expansion for stdio is deliberately preserved.** A stdio server that loses a variable dies visibly at spawn with its stderr captured. Only *remote* configs get the strict treatment. Do not "fix" stdio's silent-empty behaviour; it is out of scope.

---

### Task 1: `env_refs` — extract expansion, add a strict variant

Move the existing `${VAR}` machinery out of `mcp_stdio.py` into its own module and add `expand_config()`, which walks an MCP server config and raises `MissingEnvVar` on a reference to an unset variable.

**Files:**
- Create: `src/jean/plugins/env_refs.py`
- Modify: `src/jean/plugins/mcp_stdio.py` (delete `_ENV_REF` at :23 and `expand()` at :40-42; import from the new module; drop the now-unused `import re`)
- Test: `tests/test_env_refs.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces, all importable from `jean.plugins.env_refs`:
  - `ENV_REF: re.Pattern[str]` — the `${NAME}` regex.
  - `expand(value: str) -> str` — lenient. Unset variable → `""`. This is the *existing* behaviour, moved verbatim; `mcp_stdio.spawn_stdio_server` keeps using it.
  - `class MissingEnvVar(Exception)` — raised by `expand_config`.
  - `expand_config(config: dict[str, Any], *, server: str) -> dict[str, Any]` — strict. Returns a new config with every `${VAR}` in every nested string replaced. Raises `MissingEnvVar` if a referenced variable is unset. Non-string leaves (int, bool, None) pass through untouched.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_env_refs.py`:

```python
from __future__ import annotations

import pytest

from jean.plugins.env_refs import MissingEnvVar, expand, expand_config


def test_lenient_expand_blanks_an_unset_var(monkeypatch):
    """stdio's behaviour, preserved verbatim: a stdio server that loses a var
    dies visibly at spawn, so blanking it there is survivable."""
    monkeypatch.delenv("JEAN_TEST_ABSENT", raising=False)
    monkeypatch.setenv("JEAN_TEST_URL", "https://es.internal")

    assert expand("${JEAN_TEST_URL}/health") == "https://es.internal/health"
    assert expand("x${JEAN_TEST_ABSENT}y") == "xy"


def test_a_remote_servers_credential_comes_from_the_environment(monkeypatch):
    """The whole point: the token lives in the env (Vault -> env, like every
    other jean secret), not as a second copy inside the mounted mcp.json."""
    monkeypatch.setenv("PORTICO_ACCESS_TOKEN", "sekrit")

    expanded = expand_config(
        {
            "type": "http",
            "url": "https://portico.int.okadoc.net/mcp",
            "headers": {"Authorization": "Bearer ${PORTICO_ACCESS_TOKEN}"},
        },
        server="portico",
    )

    assert expanded["headers"]["Authorization"] == "Bearer sekrit"
    assert expanded["url"] == "https://portico.int.okadoc.net/mcp"


def test_expansion_reaches_every_nested_string(monkeypatch):
    monkeypatch.setenv("HOST", "portico.int")
    monkeypatch.setenv("TOK", "t0k")

    expanded = expand_config(
        {
            "url": "https://${HOST}/mcp",
            "headers": {"Authorization": "Bearer ${TOK}"},
            "args": ["--host", "${HOST}"],
            "timeout": 30,
            "insecure": False,
        },
        server="portico",
    )

    assert expanded == {
        "url": "https://portico.int/mcp",
        "headers": {"Authorization": "Bearer t0k"},
        "args": ["--host", "portico.int"],
        "timeout": 30,
        "insecure": False,
    }


def test_a_config_without_references_is_returned_unchanged():
    config = {"type": "http", "url": "https://x", "headers": {"A": "b"}}

    assert expand_config(config, server="remote") == config


def test_an_unset_var_in_a_remote_config_fails_loudly(monkeypatch):
    """Blanking this one would send `Authorization: Bearer ` -- jean boots clean,
    every call 401s, nothing in the logs says why, and the agent falls back to
    curl (i.e. straight back to the click storm). Refuse to boot instead."""
    monkeypatch.delenv("PORTICO_ACCESS_TOKEN", raising=False)

    with pytest.raises(MissingEnvVar) as exc:
        expand_config(
            {"headers": {"Authorization": "Bearer ${PORTICO_ACCESS_TOKEN}"}},
            server="portico",
        )

    # The message has to name both, or the operator is left grepping.
    assert "PORTICO_ACCESS_TOKEN" in str(exc.value)
    assert "portico" in str(exc.value)


def test_expansion_does_not_mutate_the_config_it_was_given(monkeypatch):
    monkeypatch.setenv("TOK", "t0k")
    config = {"headers": {"Authorization": "Bearer ${TOK}"}}

    expand_config(config, server="portico")

    assert config["headers"]["Authorization"] == "Bearer ${TOK}"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ../jean-remote-mcp-env && uv run pytest tests/test_env_refs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jean.plugins.env_refs'`

- [ ] **Step 3: Write the implementation**

Create `src/jean/plugins/env_refs.py`:

```python
from __future__ import annotations

import os
import re
from typing import Any

# `${VAR}` inside an MCP server config. The CLI expands these in a .mcp.json it
# reads itself, so jean must too for the configs it hands over -- and jean ships
# its servers to the CLI as an inline `--mcp-config` blob, not as a file on disk,
# so betting a bearer token on the CLI doing it for us is not a bet worth having.
ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class MissingEnvVar(Exception):
    """A remote MCP config referenced an env var that is not set."""


def expand(value: str) -> str:
    """Lenient: an unset var becomes "". Used for a *stdio* server's env block.

    Survivable there because a stdio server starved of a variable dies at spawn,
    loudly, with its stderr captured (mcp_stdio.stderr_tail). A remote server has
    no such moment -- see expand_config.
    """
    return ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)


def expand_config(config: dict[str, Any], *, server: str) -> dict[str, Any]:
    """Strict: every `${VAR}` in a remote server's config, or refuse to boot.

    Blanking an unset var here is the worst option available. `Authorization:
    Bearer ${PORTICO_ACCESS_TOKEN}` with the var unset becomes `Bearer `, jean
    boots clean, and every call to the server 401s with nothing in the logs
    saying why -- at which point the agent, finding its tools broken, falls back
    to curl-ing the endpoint through Bash, which is one approval click per call.
    That is the exact failure this whole change exists to remove. A credential
    misconfiguration is a deploy-time error; raise it on the deploy that caused
    it, the way a malformed JEAN_APPROVERS already does (config.py).

    Returns a new config; the caller's dict is never mutated.
    """
    return {key: _expand_value(value, server=server, path=key) for key, value in config.items()}


def _expand_value(value: Any, *, server: str, path: str) -> Any:
    if isinstance(value, str):
        return _expand_strict(value, server=server, path=path)
    if isinstance(value, dict):
        return {k: _expand_value(v, server=server, path=f"{path}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_value(v, server=server, path=f"{path}[{i}]") for i, v in enumerate(value)]
    return value


def _expand_strict(value: str, *, server: str, path: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        try:
            return os.environ[name]
        except KeyError:
            raise MissingEnvVar(
                f"MCP server {server!r}: {path} references ${{{name}}}, which is not set. "
                f"Set {name} in the environment, or drop the reference from mcp.json."
            ) from None

    return ENV_REF.sub(replace, value)
```

- [ ] **Step 4: Point `mcp_stdio` at the new module**

In `src/jean/plugins/mcp_stdio.py`: delete the `_ENV_REF` constant (line 23) and the `expand()` function (lines 40-42), delete `import re` (line 6), and add the import. `import os` **stays** — `spawn_stdio_server` still uses `os.environ` at line 46.

The import block becomes:

```python
from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from jean.plugins.env_refs import expand
```

`spawn_stdio_server` is unchanged — it still calls `expand(str(v))` on each `env` value, now resolving to the moved function. Behaviour is identical.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd ../jean-remote-mcp-env && uv run pytest tests/test_env_refs.py tests/test_mcp_client.py tests/test_mcp_config.py -v`
Expected: PASS. `test_mcp_client.py` is the regression check on the move — stdio spawning must behave exactly as before.

- [ ] **Step 6: Verify and commit**

```bash
cd ../jean-remote-mcp-env
./scripts/verify.sh
git add src/jean/plugins/env_refs.py src/jean/plugins/mcp_stdio.py tests/test_env_refs.py
git commit -m "refactor(mcp): lift env-ref expansion into its own module, with a strict variant"
```

Expected: `verify.sh` exits 0.

---

### Task 2: `remote_servers()` expands its configs

Apply the strict expander to every remote server on the way out of `remote_servers()`. This is the change that lets Portico's bearer token live in the environment — and `remote_servers()` is called from `server.py:211` at boot, so an unset variable surfaces as a boot failure, exactly as the spec requires.

**Files:**
- Modify: `src/jean/plugins/mcp_config.py:63-69` (`remote_servers`)
- Test: `tests/test_mcp_config.py` (append)

**Interfaces:**
- Consumes: `expand_config`, `MissingEnvVar` from `jean.plugins.env_refs` (Task 1).
- Produces: `remote_servers(extra_mcp: dict[str, Any]) -> dict[str, Any]` — same signature as today, now with `${VAR}` resolved in each returned config.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_config.py`:

```python
def test_a_remote_servers_credential_comes_from_the_environment(monkeypatch):
    """Portico is an MCP server, and registering it as one is what keeps it off
    the Bash/curl path -- where every call costs an approval click. Its token
    reaches it from the env, so the mounted mcp.json holds no second copy of a
    credential to rotate."""
    monkeypatch.setenv("PORTICO_ACCESS_TOKEN", "sekrit")
    extra = {
        "portico": {
            "type": "http",
            "url": "https://portico.int.okadoc.net/mcp",
            "headers": {"Authorization": "Bearer ${PORTICO_ACCESS_TOKEN}"},
        }
    }

    assert remote_servers(extra) == {
        "portico": {
            "type": "http",
            "url": "https://portico.int.okadoc.net/mcp",
            "headers": {"Authorization": "Bearer sekrit"},
        }
    }


def test_a_remote_server_missing_its_credential_fails_at_boot(monkeypatch):
    """remote_servers() runs at boot (server.py). Better a crashloop naming the
    variable than a jean that boots clean and 401s on every Portico call."""
    monkeypatch.delenv("PORTICO_ACCESS_TOKEN", raising=False)
    extra = {
        "portico": {
            "type": "http",
            "url": "https://portico.int.okadoc.net/mcp",
            "headers": {"Authorization": "Bearer ${PORTICO_ACCESS_TOKEN}"},
        }
    }

    with pytest.raises(MissingEnvVar, match="PORTICO_ACCESS_TOKEN"):
        remote_servers(extra)


def test_a_stdio_servers_command_is_not_expanded_here(monkeypatch):
    """stdio configs are expanded at spawn (mcp_stdio), not here, and only in
    their `env` block. remote_servers() must not reach into them at all -- an
    unset var in a stdio config must not take the whole pod down."""
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    extra = {"local": {"command": "npx", "args": ["${NOT_SET_ANYWHERE}"]}}

    assert remote_servers(extra) == {}
    assert stdio_servers(extra, []) == {"local": {"command": "npx", "args": ["${NOT_SET_ANYWHERE}"]}}
```

Add the imports at the top of `tests/test_mcp_config.py`:

```python
import pytest

from jean.plugins.env_refs import MissingEnvVar
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ../jean-remote-mcp-env && uv run pytest tests/test_mcp_config.py -v`
Expected: FAIL — `test_a_remote_servers_credential_comes_from_the_environment` fails on the literal `"Bearer ${PORTICO_ACCESS_TOKEN}"` still being in the output; `test_a_remote_server_missing_its_credential_fails_at_boot` fails with `DID NOT RAISE`. The third test should already pass.

- [ ] **Step 3: Write the implementation**

In `src/jean/plugins/mcp_config.py`, add the import:

```python
from jean.plugins.env_refs import expand_config
```

and replace `remote_servers` (lines 63-69) with:

```python
def remote_servers(extra_mcp: dict[str, Any]) -> dict[str, Any]:
    """The http/sse servers in mcp.json, with their `${VAR}` references resolved.

    There is no child process to share: every session's CLI just opens its own
    connection to the same remote server, which is what jean wants anyway.

    Expanded here rather than left to the CLI because the SDK ships these as an
    inline `--mcp-config` JSON blob, not as a .mcp.json on disk -- so whether the
    CLI would expand them is undocumented, and a bearer token is not the thing to
    find that out with. Expanding is idempotent: if the CLI expands too, jean has
    already substituted and there is nothing left for it to find.

    Strict, so an unset var raises at boot (env_refs.expand_config): registering a
    remote MCP server is precisely what keeps it off the Bash/curl path, where
    every call costs a human an approval click -- and a silently blank credential
    would 401 every call and send the agent right back to curl.
    """
    return {
        name: expand_config(cfg, server=name)
        for name, cfg in extra_mcp.items()
        if "command" not in cfg
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ../jean-remote-mcp-env && uv run pytest tests/test_mcp_config.py -v`
Expected: PASS, all tests in the file. Note `test_http_servers_are_left_for_the_cli_to_connect_to` (existing) must still pass — a config with no `${...}` in it comes back byte-for-byte identical.

- [ ] **Step 5: Verify and commit**

```bash
cd ../jean-remote-mcp-env
./scripts/verify.sh
git add src/jean/plugins/mcp_config.py tests/test_mcp_config.py
git commit -m "feat(mcp): expand \${VAR} in remote server configs, failing at boot on an unset one"
```

Expected: `verify.sh` exits 0.

---

### Task 3: Document the remote-server config in the README

The whole point of this change is that an operator can register Portico. Nothing in the repo currently tells them how, or that an unset variable is a boot failure rather than a warning.

**Files:**
- Modify: `README.md` (the MCP / configuration section — find it with `grep -n "mcp.json" README.md`)

- [ ] **Step 1: Find where MCP config is documented**

Run: `cd ../jean-remote-mcp-env && grep -n "mcp.json\|mcpServers\|MCP" README.md`

- [ ] **Step 2: Add the remote-server section**

Add, in the section that covers `mcp.json` (adapt the surrounding heading level to match):

````markdown
### Remote (http) MCP servers

An entry with no `command` is a remote server. jean does not run it — the CLI
connects to it directly — and **its tools are auto-allowed**, so the agent calls
them without an approval click. That is the point: an HTTP API jean is *not*
told about gets reached with `curl` through `Bash` instead, and every `Bash` call
costs a human an approval click.

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

`${VAR}` is read from jean's environment, so credentials stay in the environment
(Vault → env) rather than being copied into the mounted config. **A `${VAR}` that
is not set is a boot failure**, not a warning — jean refuses to start rather than
send an empty credential and 401 on every call.

Auto-allowed means *all* of a remote server's tools, writes included. Register
only servers whose token is scoped to what jean should be able to do.
````

- [ ] **Step 3: Verify and commit**

```bash
cd ../jean-remote-mcp-env
./scripts/verify.sh
git add README.md
git commit -m "docs: how to register a remote MCP server, and why an unset var is fatal"
```

Expected: `verify.sh` exits 0.

---

## Done means

- [ ] `./scripts/verify.sh` exits 0 on `remote-mcp-env`.
- [ ] `remote_servers()` resolves `${VAR}` in `url` and `headers`; an unset one raises `MissingEnvVar` naming both the variable and the server.
- [ ] stdio expansion is unchanged — `tests/test_mcp_client.py` passes untouched.
- [ ] Branch merged to `main`, worktree removed: `git worktree remove ../jean-remote-mcp-env`.

## Then, outside this repo — this change does nothing until both land

1. **flux-infra:** add the `portico` block to jean's mounted `mcp.json`, and make sure `PORTICO_ACCESS_TOKEN` is in jean's env (Vault/VSO) — it is what the token was already called in the failing Slack thread. Then `rollout restart` (the image tag is mutable, see the deployment notes).
2. **oka-skills, `portico` skill:** rewrite it to call the `mcp__portico__*` tools instead of teaching the `curl .../mcp` + `python3 -c` JSON-RPC pattern. **Left as-is, the skill undoes this entire change** — jean will keep curling out of habit, and every curl is an approval click.

Verify by asking jean in Slack for open SRE tickets: it should answer with **zero** approval prompts, and the transcript should show `mcp__portico__atlassian__searchJiraIssuesUsingJql` rather than a `curl`.

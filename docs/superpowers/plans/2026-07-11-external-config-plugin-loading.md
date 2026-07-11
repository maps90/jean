# External Config & Plugin Loading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let jean load its soul, extra MCP servers, and marketplace plugins from external files (Vault-mountable) instead of a fixed local identity file and a single in-process MCP server.

**Architecture:** Ports & adapters. New domain loaders parse `jean.json`/`mcp.json`; a `MarketplaceResolver` port (git adapter) clones marketplace repos at boot and returns local plugin paths; `server.py` (composition root) wires the results into `ClaudeAgentOptions`. Domain code never imports git/subprocess/network.

**Tech Stack:** Python 3.11+, `claude-agent-sdk==0.2.110`, pydantic-settings, pytest + pytest-asyncio (`asyncio_mode=auto`), asyncio subprocess for git.

## Global Constraints

- `from __future__ import annotations` at the top of every module; modern hints (`str | None`, `list[str]`).
- Async on I/O paths; domain methods touching a port are `async`.
- Dependency injection over globals; only `server.py` constructs concrete adapters.
- Domain modules (`gateway/`, `session/`, `approval/`, `persona/`, loaders) must NOT import `asyncpg`/`slack_*`/construct `ClaudeSDKClient`. The git adapter is the only place that shells out.
- Config via `JEAN_*` env → `Settings` (pydantic-settings); the two auth tokens stay unprefixed.
- Never swallow errors in domain code; a bad manifest/clone aborts startup (loud), except best-effort Slack niceties.
- Run `./scripts/verify.sh` before every commit (ruff check + format-check + pytest). Fix lint with `uv run ruff check --fix src tests` and `uv run ruff format src tests`.
- No AI co-author trailers in commits.
- SDK facts: `plugins=[{"type":"local","path":str}]` (local only); `mcp_servers` is a `dict[str, McpServerConfig] | str | Path` (merge into one dict to keep `jean_slack`); leave `strict_mcp_config=False`; enable skills via `skills="all"`.

## File Structure

- `src/jean/config.py` — add the 5 new settings + default resolution (modify).
- `src/jean/ports.py` — add `PluginRef`, `ResolvedPlugin`, `MarketplaceResolver` (modify).
- `src/jean/plugins/__init__.py` — new package (create, empty).
- `src/jean/plugins/manifest.py` — `load_plugin_manifest`, `load_mcp_config` (create).
- `src/jean/plugins/git_resolver.py` — `GitMarketplaceResolver` + default git runner (create).
- `src/jean/agent_options.py` — pure `build_agent_options(...)` helper (create).
- `src/jean/server.py` — wire loaders + resolver + helper into `run()` (modify).
- `tests/test_config.py`, `tests/test_manifest.py`, `tests/test_git_resolver.py`, `tests/test_agent_options.py`.

---

### Task 1: Config settings for external paths + token

**Files:**
- Modify: `src/jean/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.identity_path: Path`, `Settings.mcp_config_path: Path`, `Settings.plugins_path: Path`, `Settings.marketplace_cache_dir: Path`, `Settings.marketplace_token: str | None`. All path settings default under `home` when their env var is unset.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_external_paths_default_under_home(clean_env):
    clean_env.setenv("JEAN_HOME", "~/.jean")
    settings = Settings.load()
    assert settings.identity_path == Path.home() / ".jean" / "IDENTITY.md"
    assert settings.mcp_config_path == Path.home() / ".jean" / "mcp.json"
    assert settings.plugins_path == Path.home() / ".jean" / "jean.json"
    assert settings.marketplace_cache_dir == Path.home() / ".jean" / "marketplaces"
    assert settings.marketplace_token is None


def test_external_paths_override(clean_env):
    clean_env.setenv("JEAN_IDENTITY_PATH", "/etc/jean/soul.md")
    clean_env.setenv("JEAN_PLUGINS_PATH", "/etc/jean/jean.json")
    clean_env.setenv("JEAN_MCP_CONFIG_PATH", "/etc/jean/mcp.json")
    clean_env.setenv("JEAN_MARKETPLACE_TOKEN", "ghp_abc")
    settings = Settings.load()
    assert settings.identity_path == Path("/etc/jean/soul.md")
    assert settings.plugins_path == Path("/etc/jean/jean.json")
    assert settings.mcp_config_path == Path("/etc/jean/mcp.json")
    assert settings.marketplace_token == "ghp_abc"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_external_paths_default_under_home -v`
Expected: FAIL — `AttributeError`/wrong type (`identity_path` is currently a property returning `home/IDENTITY.md`, and the other attrs don't exist).

- [ ] **Step 3: Write minimal implementation**

In `src/jean/config.py`, add fields after `soul_parse_model` and DELETE the existing `identity_path` property (it becomes a field). Resolve defaults in `__init__`:

```python
    # External file paths (mountable from a Secret); default under home.
    identity_path: Path | None = None
    mcp_config_path: Path | None = None
    plugins_path: Path | None = None
    marketplace_cache_dir: Path | None = None
    marketplace_token: str | None = None
```

Update `__init__` (after `self.home = self.home.expanduser()`):

```python
        self.home = self.home.expanduser()
        self.identity_path = (self.identity_path or self.home / "IDENTITY.md").expanduser()
        self.mcp_config_path = (self.mcp_config_path or self.home / "mcp.json").expanduser()
        self.plugins_path = (self.plugins_path or self.home / "jean.json").expanduser()
        self.marketplace_cache_dir = (
            self.marketplace_cache_dir or self.home / "marketplaces"
        ).expanduser()
```

Remove the old:

```python
    @property
    def identity_path(self) -> Path:
        return self.home / "IDENTITY.md"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (including the existing `test_home_expands_under_home_dir`, which still sees `identity_path == ~/.jean/IDENTITY.md`).

- [ ] **Step 5: Commit**

```bash
git add src/jean/config.py tests/test_config.py
git commit -m "config: add external soul/mcp/plugins paths + marketplace token"
```

---

### Task 2: jean.json and mcp.json loaders

**Files:**
- Create: `src/jean/plugins/__init__.py` (empty)
- Create: `src/jean/plugins/manifest.py`
- Modify: `src/jean/ports.py` (add `PluginRef`, `ResolvedPlugin`)
- Test: `tests/test_manifest.py`

**Interfaces:**
- Produces: `PluginRef(marketplace: str, plugin: str, ref: str)` and `ResolvedPlugin(name: str, path: str)` dataclasses in `ports.py`; `load_plugin_manifest(path: Path) -> list[PluginRef]` and `load_mcp_config(path: Path) -> dict[str, Any]` in `manifest.py`. Missing file → empty; malformed → `ValueError`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_manifest.py`:

```python
from __future__ import annotations

import json

import pytest

from jean.plugins.manifest import load_mcp_config, load_plugin_manifest
from jean.ports import PluginRef


def test_load_plugin_manifest_parses_entries(tmp_path):
    p = tmp_path / "jean.json"
    p.write_text(json.dumps({"plugins": [
        {"marketplace": "git@github.com:OkadocTech/oka-skills.git", "plugin": "grafana", "ref": "main"},
    ]}))
    assert load_plugin_manifest(p) == [
        PluginRef("git@github.com:OkadocTech/oka-skills.git", "grafana", "main"),
    ]


def test_load_plugin_manifest_missing_file_is_empty(tmp_path):
    assert load_plugin_manifest(tmp_path / "absent.json") == []


def test_load_plugin_manifest_rejects_missing_field(tmp_path):
    p = tmp_path / "jean.json"
    p.write_text(json.dumps({"plugins": [{"marketplace": "x", "plugin": "grafana"}]}))
    with pytest.raises(ValueError):
        load_plugin_manifest(p)


def test_load_mcp_config_returns_servers_map(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"mcpServers": {"kubernetes": {"command": "npx", "args": ["-y", "x"]}}}))
    assert load_mcp_config(p) == {"kubernetes": {"command": "npx", "args": ["-y", "x"]}}


def test_load_mcp_config_missing_file_is_empty(tmp_path):
    assert load_mcp_config(tmp_path / "absent.json") == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_manifest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jean.plugins'`.

- [ ] **Step 3: Write minimal implementation**

Add to `src/jean/ports.py` (near the other dataclasses):

```python
@dataclass
class PluginRef:
    marketplace: str
    plugin: str
    ref: str


@dataclass
class ResolvedPlugin:
    name: str
    path: str
```

Create `src/jean/plugins/__init__.py` (empty). Create `src/jean/plugins/manifest.py`:

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jean.ports import PluginRef


def load_plugin_manifest(path: Path) -> list[PluginRef]:
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    entries = data.get("plugins", [])
    refs: list[PluginRef] = []
    for e in entries:
        try:
            refs.append(PluginRef(e["marketplace"], e["plugin"], e["ref"]))
        except (KeyError, TypeError) as exc:
            raise ValueError(f"invalid plugin entry {e!r}: {exc}") from exc
    return refs


def load_mcp_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcp.json 'mcpServers' must be an object")
    return servers
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_manifest.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/jean/ports.py src/jean/plugins/__init__.py src/jean/plugins/manifest.py tests/test_manifest.py
git commit -m "plugins: add jean.json + mcp.json loaders"
```

---

### Task 3: MarketplaceResolver port + git adapter

**Files:**
- Modify: `src/jean/ports.py` (add `MarketplaceResolver` Protocol)
- Create: `src/jean/plugins/git_resolver.py`
- Test: `tests/test_git_resolver.py`

**Interfaces:**
- Consumes: `PluginRef`, `ResolvedPlugin` (Task 2).
- Produces: `MarketplaceResolver` Protocol with `async def resolve(self, entries: list[PluginRef]) -> list[ResolvedPlugin]`; concrete `GitMarketplaceResolver(*, token: str | None, cache_dir: Path, runner: GitRunner | None = None)` where `GitRunner = Callable[[list[str], Path], Awaitable[None]]`. Clones each unique `(marketplace, ref)` once; raises `RuntimeError` if a named plugin is absent or unlisted.

- [ ] **Step 1: Write the failing test**

Create `tests/test_git_resolver.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jean.plugins.git_resolver import GitMarketplaceResolver
from jean.ports import PluginRef, ResolvedPlugin


def _make_fake_runner(plugins: list[str]):
    """Simulate `git clone` by materializing a marketplace layout in dest."""
    calls: list[list[str]] = []

    async def runner(args: list[str], cwd: Path) -> None:
        calls.append(args)
        if args[0] == "clone":
            dest = Path(args[-1])
            (dest / ".git").mkdir(parents=True, exist_ok=True)
            mp = dest / ".claude-plugin"
            mp.mkdir(parents=True, exist_ok=True)
            (mp / "marketplace.json").write_text(
                json.dumps({"plugins": [{"name": n, "source": f"./plugins/{n}"} for n in plugins]})
            )
            for n in plugins:
                (dest / "plugins" / n).mkdir(parents=True, exist_ok=True)

    return runner, calls


async def test_resolve_returns_local_paths(tmp_path):
    runner, calls = _make_fake_runner(["grafana", "kubectl"])
    r = GitMarketplaceResolver(token="ghp_x", cache_dir=tmp_path, runner=runner)
    out = await r.resolve([
        PluginRef("git@github.com:OkadocTech/oka-skills.git", "grafana", "main"),
        PluginRef("git@github.com:OkadocTech/oka-skills.git", "kubectl", "main"),
    ])
    assert [p.name for p in out] == ["grafana", "kubectl"]
    assert all(Path(p.path).is_dir() for p in out)
    # Same (marketplace, ref) cloned once, not per-plugin.
    assert sum(1 for c in calls if c[0] == "clone") == 1


async def test_token_never_in_cache_path(tmp_path):
    runner, _ = _make_fake_runner(["grafana"])
    r = GitMarketplaceResolver(token="ghp_secret", cache_dir=tmp_path, runner=runner)
    out = await r.resolve([PluginRef("git@github.com:OkadocTech/oka-skills.git", "grafana", "main")])
    assert "ghp_secret" not in out[0].path


async def test_clone_url_uses_https_token(tmp_path):
    runner, calls = _make_fake_runner(["grafana"])
    r = GitMarketplaceResolver(token="ghp_secret", cache_dir=tmp_path, runner=runner)
    await r.resolve([PluginRef("git@github.com:OkadocTech/oka-skills.git", "grafana", "main")])
    clone = next(c for c in calls if c[0] == "clone")
    url = next(a for a in clone if "github.com" in a)
    assert url == "https://x-access-token:ghp_secret@github.com/OkadocTech/oka-skills.git"


async def test_missing_plugin_raises(tmp_path):
    runner, _ = _make_fake_runner(["grafana"])  # marketplace lacks "elasticsearch"
    r = GitMarketplaceResolver(token=None, cache_dir=tmp_path, runner=runner)
    with pytest.raises(RuntimeError):
        await r.resolve([PluginRef("git@github.com:OkadocTech/oka-skills.git", "elasticsearch", "main")])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_git_resolver.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jean.plugins.git_resolver'`.

- [ ] **Step 3: Write minimal implementation**

Add to `src/jean/ports.py`:

```python
@runtime_checkable
class MarketplaceResolver(Protocol):
    async def resolve(self, entries: list[PluginRef]) -> list[ResolvedPlugin]: ...
```

Create `src/jean/plugins/git_resolver.py`:

```python
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from jean.ports import PluginRef, ResolvedPlugin

GitRunner = Callable[[list[str], Path], Awaitable[None]]

# git@github.com:ORG/repo.git  or  https://github.com/ORG/repo.git
_GH = re.compile(r"^(?:git@github\.com:|https://github\.com/)(?P<path>.+?)(?:\.git)?$")


def _auth_url(marketplace: str, token: str | None) -> str:
    m = _GH.match(marketplace)
    path = m.group("path") if m else marketplace
    if token:
        return f"https://x-access-token:{token}@github.com/{path}.git"
    return f"https://github.com/{path}.git"


def _clone_key(marketplace: str, ref: str) -> str:
    # Hash of (marketplace, ref) — never contains the token or raw URL.
    return hashlib.sha256(f"{marketplace}@{ref}".encode()).hexdigest()[:16]


async def _default_git_run(args: list[str], cwd: Path) -> None:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {err.decode(errors='replace')}")


class GitMarketplaceResolver:
    """Clones marketplace repos over HTTPS (token auth) and returns local plugin
    paths for the SDK's local-plugin loading. Fails loudly on any resolve error."""

    def __init__(self, *, token: str | None, cache_dir: Path, runner: GitRunner | None = None) -> None:
        self._token = token
        self._cache_dir = Path(cache_dir)
        self._run = runner or _default_git_run

    async def resolve(self, entries: list[PluginRef]) -> list[ResolvedPlugin]:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        clones: dict[tuple[str, str], Path] = {}
        out: list[ResolvedPlugin] = []
        for e in entries:
            key = (e.marketplace, e.ref)
            if key not in clones:
                clones[key] = await self._clone(e)
            plugin_dir = clones[key] / "plugins" / e.plugin
            self._validate(clones[key], plugin_dir, e)
            out.append(ResolvedPlugin(name=e.plugin, path=str(plugin_dir)))
        return out

    async def _clone(self, e: PluginRef) -> Path:
        dest = self._cache_dir / _clone_key(e.marketplace, e.ref)
        if not (dest / ".git").exists():
            url = _auth_url(e.marketplace, self._token)
            await self._run(["clone", "--depth", "1", "--branch", e.ref, url, str(dest)], self._cache_dir)
            # Strip the token from the persisted remote so it never lingers on disk.
            tokenless = _auth_url(e.marketplace, None)
            await self._run(["-C", str(dest), "remote", "set-url", "origin", tokenless], self._cache_dir)
        return dest

    def _validate(self, clone: Path, plugin_dir: Path, e: PluginRef) -> None:
        if not plugin_dir.is_dir():
            raise RuntimeError(f"plugin '{e.plugin}' not found in {e.marketplace}@{e.ref}")
        mp = clone / ".claude-plugin" / "marketplace.json"
        listed = {p.get("name") for p in json.loads(mp.read_text()).get("plugins", [])} if mp.exists() else set()
        if listed and e.plugin not in listed:
            raise RuntimeError(f"plugin '{e.plugin}' not listed in {e.marketplace} marketplace.json")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_git_resolver.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/jean/ports.py src/jean/plugins/git_resolver.py tests/test_git_resolver.py
git commit -m "plugins: git marketplace resolver (token clone -> local plugin paths)"
```

---

### Task 4: Build agent options + wire into server

**Files:**
- Create: `src/jean/agent_options.py`
- Modify: `src/jean/server.py`
- Test: `tests/test_agent_options.py`

**Interfaces:**
- Consumes: `ResolvedPlugin` (Task 2). The in-process MCP server object + `tool_names` from `build_slack_mcp` (existing).
- Produces: `build_agent_options(*, persona_text, slack_server, slack_tool_names, extra_mcp, plugins, settings, resume) -> ClaudeAgentOptions`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_agent_options.py`:

```python
from __future__ import annotations

from jean.agent_options import build_agent_options
from jean.config import Settings
from jean.ports import ResolvedPlugin


def _settings(monkeypatch):
    monkeypatch.setenv("JEAN_SLACK_BOT_TOKEN", "xoxb")
    monkeypatch.setenv("JEAN_SLACK_APP_TOKEN", "xapp")
    return Settings.load()


def test_merges_slack_and_external_mcp(monkeypatch):
    opts = build_agent_options(
        persona_text="I am jean.",
        slack_server={"_": "slack"},
        slack_tool_names=["mcp__jean_slack__reply"],
        extra_mcp={"kubernetes": {"command": "npx"}},
        plugins=[ResolvedPlugin("grafana", "/opt/mp/plugins/grafana")],
        settings=_settings(monkeypatch),
        resume=None,
    )
    assert opts.mcp_servers["jean_slack"] == {"_": "slack"}
    assert opts.mcp_servers["kubernetes"] == {"command": "npx"}
    assert opts.plugins == [{"type": "local", "path": "/opt/mp/plugins/grafana"}]
    assert opts.skills == "all"
    assert opts.strict_mcp_config is False


def test_no_plugins_no_extra_mcp(monkeypatch):
    opts = build_agent_options(
        persona_text="I am jean.",
        slack_server={"_": "slack"},
        slack_tool_names=["mcp__jean_slack__reply"],
        extra_mcp={},
        plugins=[],
        settings=_settings(monkeypatch),
        resume="sess-123",
    )
    assert list(opts.mcp_servers) == ["jean_slack"]
    assert opts.plugins == []
    assert opts.resume == "sess-123"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_options.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jean.agent_options'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/jean/agent_options.py`:

```python
from __future__ import annotations

from typing import Any

from claude_agent_sdk import ClaudeAgentOptions

from jean.config import Settings
from jean.persona.identity import compose_system_prompt
from jean.ports import ResolvedPlugin


async def _allow_all_tools(tool_name: str, tool_input: dict[str, Any], context: Any):
    from claude_agent_sdk import PermissionResultAllow

    del tool_name, tool_input, context
    return PermissionResultAllow()


def build_agent_options(
    *,
    persona_text: str,
    slack_server: Any,
    slack_tool_names: list[str],
    extra_mcp: dict[str, Any],
    plugins: list[ResolvedPlugin],
    settings: Settings,
    resume: str | None,
) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=compose_system_prompt(persona_text),
        mcp_servers={"jean_slack": slack_server, **extra_mcp},
        allowed_tools=[*slack_tool_names, "mcp__*"],
        plugins=[{"type": "local", "path": p.path} for p in plugins],
        skills="all",
        strict_mcp_config=False,
        permission_mode=settings.permission_mode,
        can_use_tool=_allow_all_tools,
        resume=resume,
        model=settings.model,
        cwd=str(settings.home / "workspaces"),
    )
```

> **Note (spike):** `allowed_tools` includes `"mcp__*"` so plugin/external MCP
> tools are reachable. Before merging, run jean against one real plugin (e.g.
> `grafana`) and confirm its `mcp__grafana__*` tools are callable; if the CLI
> requires exact names instead of the wildcard, replace `"mcp__*"` with the
> per-server patterns discovered at resolve time.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_agent_options.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Wire the helper into `server.py`**

In `src/jean/server.py`: remove the module-level `_allow_all_tools` (now in `agent_options.py`) and replace the inline `options_factory` + soul loading. After `store = await PostgresStore.connect(...)` and building `chat`/`gate`/`server_mcp, tool_names`, load external config and resolve plugins, then:

```python
    from jean.agent_options import build_agent_options
    from jean.plugins.git_resolver import GitMarketplaceResolver
    from jean.plugins.manifest import load_mcp_config, load_plugin_manifest

    persona_text = load_identity(settings.identity_path)
    extra_mcp = load_mcp_config(settings.mcp_config_path)
    resolver = GitMarketplaceResolver(
        token=settings.marketplace_token, cache_dir=settings.marketplace_cache_dir
    )
    plugins = await resolver.resolve(load_plugin_manifest(settings.plugins_path))

    def options_factory(resume: str | None) -> ClaudeAgentOptions:
        return build_agent_options(
            persona_text=persona_text,
            slack_server=server_mcp,
            slack_tool_names=tool_names,
            extra_mcp=extra_mcp,
            plugins=plugins,
            settings=settings,
            resume=resume,
        )
```

Ensure `load_identity` is still imported and the old `_allow_all_tools`/`compose_system_prompt` inline usage is removed (they now live in `agent_options.py`).

- [ ] **Step 6: Run the full gate**

Run: `./scripts/verify.sh`
Expected: ruff clean, format clean, all tests pass (existing + the 4 new files). If `test_server_import.py` fails on an unused import, remove it.

- [ ] **Step 7: Commit**

```bash
git add src/jean/agent_options.py src/jean/server.py tests/test_agent_options.py
git commit -m "server: load external soul/mcp/plugins into agent options"
```

---

## Self-Review

- **Spec coverage:** soul-from-path → Task 1 + server wiring (Task 4). `mcp.json`→`mcp_servers` → Tasks 2, 4. `jean.json`→clone→plugins → Tasks 2, 3, 4. Config settings → Task 1. Ports/adapters (`MarketplaceResolver`) → Task 3. Fail-loud → Tasks 2, 3. `allowed_tools` spike → Task 4 note. ✅
- **Type consistency:** `PluginRef(marketplace, plugin, ref)` and `ResolvedPlugin(name, path)` defined in Task 2, used identically in Tasks 3–4. `build_agent_options` signature matches its test. ✅
- **Placeholders:** none — every code step is concrete.
- **Out of scope (separate plan):** flux-infra manifests + Dockerfile changes (baking git into the image is already satisfied — `python:3.11-slim` includes no git; **add `git` to the Dockerfile** as a one-line follow-up in sub-project B's plan).

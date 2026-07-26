from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from jean.plugins.overlay import MANIFEST, build_overlay, resolve_within
from jean.ports import PluginRef, ResolvedPlugin

GitRunner = Callable[[list[str], Path], Awaitable[None]]

# git@github.com:ORG/repo.git  or  https://github.com/ORG/repo.git
_GH = re.compile(r"^(?:git@github\.com:|https://github\.com/)(?P<path>.+?)(?:\.git)?$")

_TOKEN_URL = re.compile(r"x-access-token:[^@/\s]+@")


def _scrub(text: str) -> str:
    """Redact any `x-access-token:<token>@` credential URL fragment.

    Token-agnostic (no token argument needed): git's verbose tracing
    (GIT_TRACE / GIT_CURL_VERBOSE / GIT_TRACE_CURL) can echo the full
    credentialed clone URL to stderr, which would otherwise land verbatim
    in exception messages and logs.
    """
    return _TOKEN_URL.sub("x-access-token:***@", text)


def _is_ssh(marketplace: str) -> bool:
    return marketplace.startswith("git@") or marketplace.startswith("ssh://")


def _auth_url(marketplace: str, token: str | None) -> str:
    # SSH transport is chosen by the URL scheme and used verbatim: git resolves
    # auth from the ambient SSH agent / deploy key, so no token is embedded (and
    # the tokenless persisted remote is identical -- the set-url step is a no-op).
    if _is_ssh(marketplace):
        return marketplace
    m = _GH.match(marketplace)
    path = m.group("path") if m else marketplace
    if token:
        return f"https://x-access-token:{token}@github.com/{path}.git"
    return f"https://github.com/{path}.git"


def _clone_key(marketplace: str, ref: str) -> str:
    # Hash of (marketplace, ref) — never contains the token or raw URL.
    return hashlib.sha256(f"{marketplace}@{ref}".encode()).hexdigest()[:16]


async def _default_git_run(args: list[str], cwd: Path) -> None:
    env = dict(os.environ)
    for key in [k for k in env if k.startswith("GIT_TRACE")]:
        del env[key]
    env.pop("GIT_CURL_VERBOSE", None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {_scrub(err.decode(errors='replace'))}")


class GitMarketplaceResolver:
    """Clones marketplace repos -- HTTPS (token auth) or SSH (ambient key),
    chosen by the marketplace URL scheme -- and returns local plugin paths for
    the SDK's local-plugin loading. Fails loudly on any resolve error."""

    def __init__(
        self, *, token: str | None, cache_dir: Path, runner: GitRunner | None = None
    ) -> None:
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
            out.append(ResolvedPlugin(name=e.plugin, path=str(self._plugin_dir(clones[key], e))))
        return out

    async def _clone(self, e: PluginRef) -> Path:
        dest = self._cache_dir / _clone_key(e.marketplace, e.ref)
        if not (dest / ".git").exists():
            url = _auth_url(e.marketplace, self._token)
            # Full clone (not --depth/--branch) so `ref` may be a branch, tag,
            # OR a commit SHA -- GitHub's smart HTTP won't fetch an arbitrary
            # shallow SHA, so we clone then check the ref out explicitly.
            await self._run(["clone", url, str(dest)], self._cache_dir)
            # Strip the token from the persisted remote so it never lingers on disk.
            tokenless = _auth_url(e.marketplace, None)
            await self._run(
                ["-C", str(dest), "remote", "set-url", "origin", tokenless], self._cache_dir
            )
            await self._run(
                ["-C", str(dest), "checkout", "--end-of-options", e.ref], self._cache_dir
            )
        return dest

    def _plugin_dir(self, clone: Path, e: PluginRef) -> Path:
        """The dir handed to the CLI as `--plugin-dir` for this entry.

        Marketplaces disagree on layout, so the manifest decides rather than a
        hardcoded path: the marketplace says `source: "./plugins/<name>"` and ships
        a `plugin.json` in each, anthropics/skills says `source: "./"` for
        every plugin and ships none. Only the second case needs an overlay
        (see plugins/overlay.py); anything already carrying its own manifest is
        handed over untouched. `plugins/<name>` stays the fallback for a
        marketplace with no manifest entry at all.
        """
        entry = self._entry(clone, e)
        source = entry.get("source") if entry else None
        if source is None:
            plugin_dir = clone / "plugins" / e.plugin
        elif isinstance(source, str):
            plugin_dir = resolve_within(clone, source)
        else:
            # A remote `source` object ({"source": "github", "repo": ...}) would
            # mean cloning a second repo; jean resolves one marketplace per entry.
            raise RuntimeError(
                f"plugin '{e.plugin}' in {e.marketplace} uses a non-local source; "
                "point jean.json at the repo that actually holds it"
            )

        if not plugin_dir.is_dir():
            raise RuntimeError(f"plugin '{e.plugin}' not found in {e.marketplace}@{e.ref}")
        if (plugin_dir / MANIFEST).is_file():
            return plugin_dir

        skills = entry.get("skills") if entry else None
        if isinstance(skills, list) and skills:
            return build_overlay(
                plugin=e.plugin,
                description=entry.get("description") if entry else None,
                source_dir=plugin_dir,
                skills=[s for s in skills if isinstance(s, str)],
                overlay_root=self._cache_dir / "overlays" / _clone_key(e.marketplace, e.ref),
            )
        # No manifest and nothing to overlay from: hand it over as-is and let
        # the CLI say what it dislikes about it.
        return plugin_dir

    def _entry(self, clone: Path, e: PluginRef) -> dict[str, Any] | None:
        mp = clone / ".claude-plugin" / "marketplace.json"
        if not mp.exists():
            return None
        plugins = json.loads(mp.read_text()).get("plugins", [])
        for p in plugins:
            if isinstance(p, dict) and p.get("name") == e.plugin:
                return p
        if plugins:
            raise RuntimeError(
                f"plugin '{e.plugin}' not listed in {e.marketplace} marketplace.json"
            )
        return None

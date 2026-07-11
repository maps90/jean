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
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {err.decode(errors='replace')}")


class GitMarketplaceResolver:
    """Clones marketplace repos over HTTPS (token auth) and returns local plugin
    paths for the SDK's local-plugin loading. Fails loudly on any resolve error."""

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
            plugin_dir = clones[key] / "plugins" / e.plugin
            self._validate(clones[key], plugin_dir, e)
            out.append(ResolvedPlugin(name=e.plugin, path=str(plugin_dir)))
        return out

    async def _clone(self, e: PluginRef) -> Path:
        dest = self._cache_dir / _clone_key(e.marketplace, e.ref)
        if not (dest / ".git").exists():
            url = _auth_url(e.marketplace, self._token)
            await self._run(
                ["clone", "--depth", "1", "--branch", e.ref, url, str(dest)], self._cache_dir
            )
            # Strip the token from the persisted remote so it never lingers on disk.
            tokenless = _auth_url(e.marketplace, None)
            await self._run(
                ["-C", str(dest), "remote", "set-url", "origin", tokenless], self._cache_dir
            )
        return dest

    def _validate(self, clone: Path, plugin_dir: Path, e: PluginRef) -> None:
        if not plugin_dir.is_dir():
            raise RuntimeError(f"plugin '{e.plugin}' not found in {e.marketplace}@{e.ref}")
        mp = clone / ".claude-plugin" / "marketplace.json"
        listed = (
            {p.get("name") for p in json.loads(mp.read_text()).get("plugins", [])}
            if mp.exists()
            else set()
        )
        if listed and e.plugin not in listed:
            raise RuntimeError(
                f"plugin '{e.plugin}' not listed in {e.marketplace} marketplace.json"
            )

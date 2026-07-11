from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

import pytest

from jean.plugins.git_resolver import GitMarketplaceResolver
from jean.ports import PluginRef


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
    out = await r.resolve(
        [
            PluginRef("git@github.com:OkadocTech/oka-skills.git", "grafana", "main"),
            PluginRef("git@github.com:OkadocTech/oka-skills.git", "kubectl", "main"),
        ]
    )
    assert [p.name for p in out] == ["grafana", "kubectl"]
    assert all(Path(p.path).is_dir() for p in out)
    # Same (marketplace, ref) cloned once, not per-plugin.
    assert sum(1 for c in calls if c[0] == "clone") == 1


async def test_token_never_in_cache_path(tmp_path):
    runner, _ = _make_fake_runner(["grafana"])
    r = GitMarketplaceResolver(token="ghp_secret", cache_dir=tmp_path, runner=runner)
    out = await r.resolve(
        [PluginRef("git@github.com:OkadocTech/oka-skills.git", "grafana", "main")]
    )
    assert "ghp_secret" not in out[0].path


async def test_clone_url_uses_https_token(tmp_path):
    runner, calls = _make_fake_runner(["grafana"])
    r = GitMarketplaceResolver(token="ghp_secret", cache_dir=tmp_path, runner=runner)
    await r.resolve([PluginRef("git@github.com:OkadocTech/oka-skills.git", "grafana", "main")])
    clone = next(c for c in calls if c[0] == "clone")
    url = next(a for a in clone if urlparse(a).hostname == "github.com")
    assert url == "https://x-access-token:ghp_secret@github.com/OkadocTech/oka-skills.git"


def test_scrub_removes_token():
    from jean.plugins.git_resolver import _scrub

    leaked = "fatal: unable to access 'https://x-access-token:ghp_secret123@github.com/o/r.git/'"
    out = _scrub(leaked)
    assert "ghp_secret123" not in out
    assert "x-access-token:***@" in out


async def test_clone_checks_out_ref(tmp_path):
    runner, calls = _make_fake_runner(["grafana"])
    r = GitMarketplaceResolver(token="ghp_x", cache_dir=tmp_path, runner=runner)
    await r.resolve([PluginRef("git@github.com:OkadocTech/oka-skills.git", "grafana", "v1.2.3")])
    checkout = next(c for c in calls if "checkout" in c)
    assert checkout[-1] == "v1.2.3"


async def test_missing_plugin_raises(tmp_path):
    runner, _ = _make_fake_runner(["grafana"])  # marketplace lacks "elasticsearch"
    r = GitMarketplaceResolver(token=None, cache_dir=tmp_path, runner=runner)
    with pytest.raises(RuntimeError):
        await r.resolve(
            [PluginRef("git@github.com:OkadocTech/oka-skills.git", "elasticsearch", "main")]
        )

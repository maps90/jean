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
            PluginRef("git@github.com:example-org/skills.git", "grafana", "main"),
            PluginRef("git@github.com:example-org/skills.git", "kubectl", "main"),
        ]
    )
    assert [p.name for p in out] == ["grafana", "kubectl"]
    assert all(Path(p.path).is_dir() for p in out)
    # Same (marketplace, ref) cloned once, not per-plugin.
    assert sum(1 for c in calls if c[0] == "clone") == 1


async def test_token_never_in_cache_path(tmp_path):
    runner, _ = _make_fake_runner(["grafana"])
    r = GitMarketplaceResolver(token="ghp_secret", cache_dir=tmp_path, runner=runner)
    out = await r.resolve([PluginRef("git@github.com:example-org/skills.git", "grafana", "main")])
    assert "ghp_secret" not in out[0].path


async def test_clone_url_uses_https_token(tmp_path):
    runner, calls = _make_fake_runner(["grafana"])
    r = GitMarketplaceResolver(token="ghp_secret", cache_dir=tmp_path, runner=runner)
    await r.resolve([PluginRef("https://github.com/example-org/skills.git", "grafana", "main")])
    clone = next(c for c in calls if c[0] == "clone")
    url = next(a for a in clone if urlparse(a).hostname == "github.com")
    assert url == "https://x-access-token:ghp_secret@github.com/example-org/skills.git"


async def test_ssh_marketplace_clones_over_ssh(tmp_path):
    # An `git@github.com:...` (or `ssh://`) marketplace must clone over SSH
    # verbatim -- transport is chosen by the URL scheme, not silently rewritten
    # to HTTPS. The HTTPS access token must never be embedded in an SSH clone.
    runner, calls = _make_fake_runner(["grafana"])
    r = GitMarketplaceResolver(token="ghp_secret", cache_dir=tmp_path, runner=runner)
    await r.resolve([PluginRef("git@github.com:example-org/skills.git", "grafana", "main")])
    clone = next(c for c in calls if c[0] == "clone")
    assert clone[1] == "git@github.com:example-org/skills.git"
    assert not any("x-access-token" in a for a in clone)
    # The persisted remote stays the same SSH url (no token to strip).
    seturl = next(c for c in calls if "set-url" in c)
    assert seturl[-1] == "git@github.com:example-org/skills.git"


def test_scrub_removes_token():
    from jean.plugins.git_resolver import _scrub

    leaked = "fatal: unable to access 'https://x-access-token:ghp_secret123@github.com/o/r.git/'"
    out = _scrub(leaked)
    assert "ghp_secret123" not in out
    assert "x-access-token:***@" in out


async def test_clone_checks_out_ref(tmp_path):
    runner, calls = _make_fake_runner(["grafana"])
    r = GitMarketplaceResolver(token="ghp_x", cache_dir=tmp_path, runner=runner)
    await r.resolve([PluginRef("git@github.com:example-org/skills.git", "grafana", "v1.2.3")])
    checkout = next(c for c in calls if "checkout" in c)
    assert checkout[-1] == "v1.2.3"


async def test_missing_plugin_raises(tmp_path):
    runner, _ = _make_fake_runner(["grafana"])  # marketplace lacks "elasticsearch"
    r = GitMarketplaceResolver(token=None, cache_dir=tmp_path, runner=runner)
    with pytest.raises(RuntimeError):
        await r.resolve(
            [PluginRef("git@github.com:example-org/skills.git", "elasticsearch", "main")]
        )


def _make_inline_runner(entries: list[dict], skills: list[str]):
    """Simulate anthropics/skills: every plugin declared inline in
    marketplace.json against `source: "./"`, with no plugin.json anywhere."""

    async def runner(args: list[str], cwd: Path) -> None:
        if args[0] != "clone":
            return
        dest = Path(args[-1])
        (dest / ".git").mkdir(parents=True, exist_ok=True)
        mp = dest / ".claude-plugin"
        mp.mkdir(parents=True, exist_ok=True)
        (mp / "marketplace.json").write_text(json.dumps({"plugins": entries}))
        for s in skills:
            d = dest / "skills" / s
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(f"---\nname: {s}\n---\n")

    return runner


async def test_source_pointing_at_repo_root_resolves_to_an_overlay(tmp_path):
    entries = [
        {
            "name": "document-skills",
            "source": "./",
            "description": "docs",
            "skills": ["./skills/docx", "./skills/xlsx"],
        },
        {"name": "example-skills", "source": "./", "skills": ["./skills/frontend-design"]},
    ]
    runner = _make_inline_runner(entries, ["docx", "xlsx", "pdf", "frontend-design"])
    r = GitMarketplaceResolver(token=None, cache_dir=tmp_path, runner=runner)
    out = await r.resolve(
        [
            PluginRef("https://github.com/anthropics/skills.git", "document-skills", "main"),
            PluginRef("https://github.com/anthropics/skills.git", "example-skills", "main"),
        ]
    )

    docs, examples = (Path(p.path) for p in out)
    assert docs != examples, "plugins sharing `source: ./` must not share a dir"
    for p in (docs, examples):
        assert (p / ".claude-plugin" / "plugin.json").is_file()
    assert sorted(d.name for d in (docs / "skills").iterdir()) == ["docx", "xlsx"]
    assert [d.name for d in (examples / "skills").iterdir()] == ["frontend-design"]


async def test_plugin_with_its_own_manifest_is_used_verbatim(tmp_path):
    """A marketplace may ship a real plugin.json per plugin -- never overlay those."""

    async def runner(args: list[str], cwd: Path) -> None:
        if args[0] != "clone":
            return
        dest = Path(args[-1])
        (dest / ".git").mkdir(parents=True, exist_ok=True)
        mp = dest / ".claude-plugin"
        mp.mkdir(parents=True, exist_ok=True)
        (mp / "marketplace.json").write_text(
            json.dumps({"plugins": [{"name": "kubectl", "source": "./plugins/kubectl"}]})
        )
        pdir = dest / "plugins" / "kubectl" / ".claude-plugin"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "plugin.json").write_text(json.dumps({"name": "kubectl"}))

    r = GitMarketplaceResolver(token=None, cache_dir=tmp_path, runner=runner)
    out = await r.resolve([PluginRef("git@github.com:example-org/skills.git", "kubectl", "main")])
    assert Path(out[0].path).name == "kubectl"
    assert Path(out[0].path).parent.name == "plugins"


def _make_token_rejecting_runner(plugins: list[str]):
    """A marketplace the token has no grant on -- a repo-scoped installation
    token or fine-grained PAT gets 'Repository not found' on any other repo,
    public or not."""
    calls: list[list[str]] = []
    base_runner, _ = _make_fake_runner(plugins)

    async def runner(args: list[str], cwd: Path) -> None:
        calls.append(args)
        if args[0] == "clone" and any("x-access-token" in a for a in args):
            dest = Path(args[-1])
            dest.mkdir(parents=True, exist_ok=True)  # git leaves a partial dir behind
            raise RuntimeError("git clone failed: remote: Repository not found.")
        await base_runner(args, cwd)

    return runner, calls


async def test_public_marketplace_retries_anonymously_when_the_token_is_rejected(tmp_path):
    runner, calls = _make_token_rejecting_runner(["document-skills"])
    r = GitMarketplaceResolver(token="ghp_scoped_to_another_org", cache_dir=tmp_path, runner=runner)
    out = await r.resolve(
        [PluginRef("https://github.com/anthropics/skills.git", "document-skills", "main")]
    )

    assert Path(out[0].path).is_dir()
    clones = [c for c in calls if c[0] == "clone"]
    assert len(clones) == 2
    assert any("x-access-token" in a for a in clones[0])
    assert not any("x-access-token" in a for a in clones[1])


async def test_tokenless_clone_failure_is_not_retried(tmp_path):
    """Nothing to fall back to -- the second attempt would be the same url."""
    calls: list[list[str]] = []

    async def runner(args: list[str], cwd: Path) -> None:
        calls.append(args)
        raise RuntimeError("git clone failed: host unreachable")

    r = GitMarketplaceResolver(token=None, cache_dir=tmp_path, runner=runner)
    with pytest.raises(RuntimeError, match="host unreachable"):
        await r.resolve([PluginRef("https://github.com/x/y.git", "p", "main")])
    assert len([c for c in calls if c[0] == "clone"]) == 1


async def test_ssh_clone_failure_is_not_retried(tmp_path):
    """An SSH url carries no token to drop, so there is no anonymous retry --
    and silently switching transports would be a surprise."""
    calls: list[list[str]] = []

    async def runner(args: list[str], cwd: Path) -> None:
        calls.append(args)
        raise RuntimeError("git clone failed: Permission denied (publickey).")

    r = GitMarketplaceResolver(token="ghp_x", cache_dir=tmp_path, runner=runner)
    with pytest.raises(RuntimeError, match="publickey"):
        await r.resolve([PluginRef("git@github.com:example-org/skills.git", "kubectl", "main")])
    assert len([c for c in calls if c[0] == "clone"]) == 1


async def test_source_escaping_the_clone_raises(tmp_path):
    runner = _make_inline_runner([{"name": "evil", "source": "../../etc"}], [])
    r = GitMarketplaceResolver(token=None, cache_dir=tmp_path, runner=runner)
    with pytest.raises(RuntimeError, match="escapes"):
        await r.resolve([PluginRef("https://github.com/x/y.git", "evil", "main")])

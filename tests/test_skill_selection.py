from __future__ import annotations

import json
from pathlib import Path

import pytest

from jean.plugins.git_resolver import GitMarketplaceResolver
from jean.plugins.manifest import load_plugin_manifest
from jean.ports import PluginRef

MARKETPLACE = "https://github.com/anthropics/skills.git"


def _inline_runner(entries: list[dict], skills: list[str], *, own_manifest: bool = False):
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
        if own_manifest:
            (mp / "plugin.json").write_text(json.dumps({"name": entries[0]["name"]}))

    return runner


def _doc_entry(*paths: str) -> dict:
    return {"name": "document-skills", "source": "./", "skills": list(paths)}


async def test_jean_json_can_narrow_a_bundled_plugin(tmp_path):
    """Upstream bundles pdf in with docx/xlsx/pptx; a deployment that wants
    only three of the four should not have to take the fourth."""
    runner = _inline_runner(
        [_doc_entry("./skills/docx", "./skills/xlsx", "./skills/pptx", "./skills/pdf")],
        ["docx", "xlsx", "pptx", "pdf"],
    )
    r = GitMarketplaceResolver(token=None, cache_dir=tmp_path, runner=runner)
    out = await r.resolve(
        [PluginRef(MARKETPLACE, "document-skills", "main", skills=("docx", "xlsx", "pptx"))]
    )

    loaded = sorted(d.name for d in (Path(out[0].path) / "skills").iterdir())
    assert loaded == ["docx", "pptx", "xlsx"]


async def test_no_filter_still_takes_everything_the_marketplace_lists(tmp_path):
    runner = _inline_runner(
        [_doc_entry("./skills/docx", "./skills/pdf")],
        ["docx", "pdf"],
    )
    r = GitMarketplaceResolver(token=None, cache_dir=tmp_path, runner=runner)
    out = await r.resolve([PluginRef(MARKETPLACE, "document-skills", "main")])

    assert sorted(d.name for d in (Path(out[0].path) / "skills").iterdir()) == ["docx", "pdf"]


async def test_unknown_skill_name_is_a_boot_error(tmp_path):
    """A typo must not silently load fewer skills than the operator asked for."""
    runner = _inline_runner([_doc_entry("./skills/docx")], ["docx"])
    r = GitMarketplaceResolver(token=None, cache_dir=tmp_path, runner=runner)
    with pytest.raises(RuntimeError, match="doxc"):
        await r.resolve([PluginRef(MARKETPLACE, "document-skills", "main", skills=("doxc",))])


async def test_filtering_a_plugin_that_ships_its_own_manifest_is_refused(tmp_path):
    """Narrowing works by rebuilding the plugin dir. A plugin that brings its
    own manifest may also bring commands, agents and MCP servers that the
    rebuild would drop -- refuse loudly instead of silently amputating it."""
    entries = [{"name": "kubectl", "source": "./", "skills": ["./skills/docx"]}]
    runner = _inline_runner(entries, ["docx"], own_manifest=True)
    r = GitMarketplaceResolver(token=None, cache_dir=tmp_path, runner=runner)
    with pytest.raises(RuntimeError, match="own plugin.json"):
        await r.resolve([PluginRef(MARKETPLACE, "kubectl", "main", skills=("docx",))])


def test_manifest_reads_the_skills_filter(tmp_path):
    p = tmp_path / "jean.json"
    p.write_text(
        json.dumps(
            {
                "plugins": [
                    {
                        "marketplace": MARKETPLACE,
                        "plugin": "document-skills",
                        "ref": "main",
                        "skills": ["docx", "xlsx", "pptx"],
                    },
                    {"marketplace": MARKETPLACE, "plugin": "example-skills", "ref": "main"},
                ]
            }
        )
    )
    refs = load_plugin_manifest(p)
    assert refs[0].skills == ("docx", "xlsx", "pptx")
    assert refs[1].skills is None


def test_manifest_rejects_a_skill_name_that_is_a_path(tmp_path):
    p = tmp_path / "jean.json"
    p.write_text(
        json.dumps(
            {
                "plugins": [
                    {
                        "marketplace": MARKETPLACE,
                        "plugin": "document-skills",
                        "ref": "main",
                        "skills": ["../../etc/passwd"],
                    }
                ]
            }
        )
    )
    with pytest.raises(ValueError, match="unsafe"):
        load_plugin_manifest(p)

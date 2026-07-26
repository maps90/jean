from __future__ import annotations

import json
from pathlib import Path

import pytest

from jean.plugins.overlay import build_overlay, resolve_within


def _skill(root: Path, name: str) -> Path:
    d = root / "skills" / name
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n")
    (d / "scripts" / "run.py").write_text("print('hi')\n")
    return d


def test_overlay_is_a_loadable_plugin(tmp_path):
    src = tmp_path / "clone"
    _skill(src, "docx")
    _skill(src, "xlsx")
    _skill(src, "pdf")

    out = build_overlay(
        plugin="document-skills",
        description="Word, Excel, PowerPoint",
        source_dir=src,
        skills=["./skills/docx", "./skills/xlsx"],
        overlay_root=tmp_path / "overlays",
    )

    manifest = json.loads((out / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "document-skills"
    assert manifest["description"] == "Word, Excel, PowerPoint"
    # Only the listed skills come along -- `pdf` was not requested.
    assert sorted(p.name for p in (out / "skills").iterdir()) == ["docx", "xlsx"]
    # Skill payloads are copied whole, not just SKILL.md.
    assert (out / "skills" / "docx" / "scripts" / "run.py").is_file()


def test_two_plugins_sharing_one_source_do_not_collide(tmp_path):
    """anthropics/skills defines every plugin with `source: "./"` -- the repo
    root. Overlays must be per-plugin or the second would clobber the first."""
    src = tmp_path / "clone"
    _skill(src, "docx")
    _skill(src, "frontend-design")
    overlays = tmp_path / "overlays"

    docs = build_overlay(
        plugin="document-skills",
        description=None,
        source_dir=src,
        skills=["./skills/docx"],
        overlay_root=overlays,
    )
    examples = build_overlay(
        plugin="example-skills",
        description=None,
        source_dir=src,
        skills=["./skills/frontend-design"],
        overlay_root=overlays,
    )

    assert docs != examples
    assert [p.name for p in (docs / "skills").iterdir()] == ["docx"]
    assert [p.name for p in (examples / "skills").iterdir()] == ["frontend-design"]


def test_rebuild_drops_skills_that_left_the_marketplace(tmp_path):
    """A boot after the marketplace ref moved must not keep serving a skill the
    new manifest no longer lists."""
    src = tmp_path / "clone"
    _skill(src, "docx")
    _skill(src, "xlsx")
    overlays = tmp_path / "overlays"

    build_overlay(
        plugin="document-skills",
        description=None,
        source_dir=src,
        skills=["./skills/docx", "./skills/xlsx"],
        overlay_root=overlays,
    )
    out = build_overlay(
        plugin="document-skills",
        description=None,
        source_dir=src,
        skills=["./skills/docx"],
        overlay_root=overlays,
    )

    assert [p.name for p in (out / "skills").iterdir()] == ["docx"]


def test_skill_without_skill_md_raises(tmp_path):
    src = tmp_path / "clone"
    (src / "skills" / "empty").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="SKILL.md"):
        build_overlay(
            plugin="p",
            description=None,
            source_dir=src,
            skills=["./skills/empty"],
            overlay_root=tmp_path / "overlays",
        )


def test_skill_path_escaping_the_source_raises(tmp_path):
    src = tmp_path / "clone"
    _skill(src, "docx")
    (tmp_path / "outside").mkdir()
    with pytest.raises(RuntimeError, match="escapes"):
        build_overlay(
            plugin="p",
            description=None,
            source_dir=src,
            skills=["../outside"],
            overlay_root=tmp_path / "overlays",
        )


def test_resolve_within_allows_dot_and_subdirs(tmp_path):
    (tmp_path / "plugins" / "kubectl").mkdir(parents=True)
    assert resolve_within(tmp_path, "./") == tmp_path.resolve()
    assert resolve_within(tmp_path, "./plugins/kubectl") == (tmp_path / "plugins" / "kubectl")


def test_resolve_within_rejects_traversal_and_absolute(tmp_path):
    with pytest.raises(RuntimeError, match="escapes"):
        resolve_within(tmp_path, "../elsewhere")
    with pytest.raises(RuntimeError, match="escapes"):
        resolve_within(tmp_path, "/etc")

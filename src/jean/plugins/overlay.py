from __future__ import annotations

import json
import shutil
from pathlib import Path

# A plugin dir is loadable by the CLI only if it holds this manifest.
MANIFEST = Path(".claude-plugin") / "plugin.json"


def resolve_within(root: Path, relative: str) -> Path:
    """Resolve `relative` under `root`, refusing anything that leaves it.

    `.resolve()` first so a symlink pointing out of the clone is caught too,
    not just a literal `../`. An absolute path is rejected for the same reason
    a traversal is: a marketplace manifest must only ever name its own repo.
    """
    root = root.resolve()
    out = (root / relative).resolve()
    if out != root and root not in out.parents:
        raise RuntimeError(f"path {relative!r} escapes the marketplace clone")
    return out


def build_overlay(
    *,
    plugin: str,
    description: str | None,
    source_dir: Path,
    skills: list[str],
    overlay_root: Path,
) -> Path:
    """Materialize a loadable plugin dir for a marketplace-inline plugin.

    Some marketplaces (anthropics/skills) never ship a `plugin.json`: the whole
    plugin is declared in `marketplace.json` as a `source` plus a list of skill
    paths, and several plugins share one `source` (`"./"`, the repo root). The
    CLI cannot load that -- `--plugin-dir` wants a dir with its own manifest,
    and one manifest at a shared root could only ever describe one of them.

    So jean builds the dir the CLI expects: a private per-plugin overlay under
    the plugin cache, holding a generated manifest and a copy of exactly the
    skills the marketplace listed. Copies (not symlinks) because the CLI walks
    this tree, and exactly-the-listed-skills because the shared root holds
    every *other* plugin's skills too -- auto-loading the source itself would
    hand every session all of them.

    Rebuilt from scratch on each call: the overlay is derived state, and a
    stale skill left behind after the marketplace `ref` moved would keep being
    served long after its manifest stopped listing it.
    """
    source_dir = source_dir.resolve()
    dest = overlay_root / plugin
    shutil.rmtree(dest, ignore_errors=True)
    (dest / MANIFEST.parent).mkdir(parents=True)

    manifest: dict[str, str] = {"name": plugin}
    if description:
        manifest["description"] = description
    (dest / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")

    for rel in skills:
        src = resolve_within(source_dir, rel)
        if not (src / "SKILL.md").is_file():
            raise RuntimeError(f"plugin '{plugin}': {rel} has no SKILL.md")
        # symlinks=True: copy links verbatim rather than following them out of
        # the clone and pulling an arbitrary tree into the overlay.
        shutil.copytree(src, dest / "skills" / src.name, symlinks=True)

    return dest

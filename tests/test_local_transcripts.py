from __future__ import annotations

from pathlib import Path

from jean.session.transcript import LocalTranscripts


def test_path_matches_the_cli_slug_formula(tmp_path: Path):
    # Verified against the real CLI: cwd `/Users/d/.jean/workspaces` lands in
    # `~/.claude/projects/-Users-d--jean-workspaces/`. Both `/` and `.` become `-`.
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/Users/d/.jean/workspaces"))

    assert local.path("abc-123") == (
        tmp_path / ".claude" / "projects" / "-Users-d--jean-workspaces" / "abc-123.jsonl"
    )


def test_write_read_delete_round_trip(tmp_path: Path):
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))

    assert local.read("sid-1") is None  # nothing on disk yet

    local.write("sid-1", b'{"type":"user"}\n')  # creates parent dirs
    assert local.read("sid-1") == b'{"type":"user"}\n'

    local.delete("sid-1")
    assert local.read("sid-1") is None

    local.delete("sid-1")  # deleting a missing transcript is not an error

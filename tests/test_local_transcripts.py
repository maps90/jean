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


def test_sample_counts_assistant_records_and_tracks_size(tmp_path: Path):
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))

    assert local.sample("sid-1") == (0, 0)  # no file

    # a real turn: several `assistant` records (a tool_use, then the answer's text)
    data = (
        b'{"type":"user"}\n'
        b'{"type":"assistant","tool_use":"mcp__jean_slack__reply"}\n'
        b'{"type":"user","tool_result":"ok"}\n'
        b'{"type":"assistant","text":"hi"}\n'
        b'{"type":"system"}\n'
    )
    local.write("sid-1", data)

    assert local.sample("sid-1") == (len(data), 2)


def test_sample_recounts_only_when_the_file_has_grown(tmp_path: Path):
    """The settle poll runs this every ~100ms against a transcript of up to 32 MB, on
    the event loop's thread pool. The file is append-only, so an unchanged size means
    an unchanged count: a poll that sees no growth must not re-read the whole file."""
    reads: list[str] = []

    class CountingTranscripts(LocalTranscripts):
        def read(self, sdk_session_id: str) -> bytes | None:
            reads.append(sdk_session_id)
            return super().read(sdk_session_id)

    local = CountingTranscripts(cli_home=tmp_path, cwd=Path("/w"))
    local.write("sid-1", b'{"type":"assistant"}\n')

    assert local.sample("sid-1") == (21, 1)
    reads.clear()

    for _ in range(5):  # the quiet window: nothing is being written
        assert local.sample("sid-1") == (21, 1)
    assert reads == [], "re-scanned a file that had not changed"

    local.write("sid-1", b'{"type":"assistant"}\n{"type":"assistant"}\n')  # it grows
    assert local.sample("sid-1") == (42, 2)
    assert len(reads) == 1


def test_sample_forgets_a_file_that_was_replaced_or_deleted(tmp_path: Path):
    """The cache leans on the file being append-only. Hydration REPLACES it and
    delete() removes it -- and a hydrated blob can be the same length as the file it
    overwrote, so size alone would not notice."""
    local = LocalTranscripts(cli_home=tmp_path, cwd=Path("/w"))

    local.write("sid-1", b'{"type":"assistant"}\n')
    assert local.sample("sid-1") == (21, 1)

    local.write("sid-1", b'{"type":"user"}aaaaa\n')  # SAME length, no assistant record
    assert local.sample("sid-1") == (21, 0)

    local.delete("sid-1")
    assert local.sample("sid-1") == (0, 0)

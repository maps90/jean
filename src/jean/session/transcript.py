from __future__ import annotations

import re
from pathlib import Path


class LocalTranscripts:
    """The claude CLI's transcript files on *this* pod's disk.

    The CLI writes each conversation to `$HOME/.claude/projects/<slug>/<id>.jsonl`,
    where the slug is its `cwd` with every `/` and `.` turned into `-`. jean gives
    every thread the same `cwd` (settings.home/"workspaces"), so there is exactly one
    project directory and transcripts differ only by session id.

    That file is the whole of a session's state: dropping it into a project dir the
    CLI has never seen and resuming by id replays the conversation. Postgres holds the
    durable copy (TranscriptStore); this class is only the local materialization.
    """

    def __init__(self, cli_home: Path, cwd: Path) -> None:
        self._dir = cli_home / ".claude" / "projects" / self._slug(cwd)

    @staticmethod
    def _slug(cwd: Path) -> str:
        return re.sub(r"[/.]", "-", str(cwd))

    def path(self, sdk_session_id: str) -> Path:
        return self._dir / f"{sdk_session_id}.jsonl"

    def read(self, sdk_session_id: str) -> bytes | None:
        path = self.path(sdk_session_id)
        if not path.exists():
            return None
        return path.read_bytes()

    def write(self, sdk_session_id: str, data: bytes) -> None:
        path = self.path(sdk_session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def delete(self, sdk_session_id: str) -> None:
        self.path(sdk_session_id).unlink(missing_ok=True)

from __future__ import annotations

import re
from pathlib import Path

# Every turn the CLI completes ends with an `assistant` record -- even one whose
# visible output came out of a tool. Counting the marker in the raw bytes is
# deliberate: JeanSession polls this while waiting for a turn to land on disk, so
# it must stay a single read with no JSON parsing.
_ASSISTANT_RECORD = b'"type":"assistant"'


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

    def assistant_records(self, sdk_session_id: str) -> int:
        """How many `assistant` records the transcript holds; 0 when there is no file.

        A turn's answer is on disk once this count has risen -- that is the signal
        JeanSession waits for before archiving (the CLI's writes lag the response by
        ~0.5s). Records, never text: a turn that answered through a tool still ends
        in an `assistant` record but need not carry the user's words.
        """
        data = self.read(sdk_session_id)
        if data is None:
            return 0
        return data.count(_ASSISTANT_RECORD)

    def size(self, sdk_session_id: str) -> int:
        """Bytes currently on disk; 0 when there is no file. A size that stops
        changing is how JeanSession knows the CLI has stopped writing."""
        try:
            return self.path(sdk_session_id).stat().st_size
        except FileNotFoundError:
            return 0

    def write(self, sdk_session_id: str, data: bytes) -> None:
        path = self.path(sdk_session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def delete(self, sdk_session_id: str) -> None:
        self.path(sdk_session_id).unlink(missing_ok=True)

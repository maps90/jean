from __future__ import annotations

import re
from pathlib import Path

# One turn writes SEVERAL of these -- the CLI emits an `assistant` record per
# assistant message (thinking, each tool_use, and finally the answer's text), and
# flushes the early ones mid-turn. So the count is only ever meaningful against an
# exact target (see JeanSession._settle), never as "has it gone up yet".
#
# Counting the marker in the raw bytes is deliberate: jean polls this while waiting
# for a turn to land on disk, so it must stay a single read with no JSON parsing.
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
        # sdk_session_id -> the last (size, assistant records) reading taken of it (see
        # sample()). Per-worker, purely derived from the file, and thrown away with the
        # file: never state anything reads for correctness.
        self._counts: dict[str, tuple[int, int]] = {}

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

    def size(self, sdk_session_id: str) -> int:
        """Bytes currently on disk; 0 when there is no file. A size that stops
        changing is how JeanSession knows the CLI has stopped writing."""
        try:
            return self.path(sdk_session_id).stat().st_size
        except FileNotFoundError:
            return 0

    def sample(self, sdk_session_id: str) -> tuple[int, int]:
        """One (size in bytes, `assistant` records) reading; (0, 0) with no file.

        Both halves of the settle predicate come from here, and they come from the
        SAME reading so they cannot disagree about which bytes they describe.

        The count is CACHED against the size, and that is not an optimization
        detail -- it is what keeps the settle poll off the event loop's back. jean
        polls this every ~100ms while a turn's write-behind lands, and a transcript
        may be up to `transcript_max_mb` (32 MB): re-scanning all of it on every
        poll would stall every other thread's turn on this worker, and the approval
        LISTEN/NOTIFY wakeup with them. The file is APPEND-ONLY, so the count cannot
        change unless the size does: stat first, and re-read only when the size has
        actually moved. The quiet window at the end of a settle -- the common case,
        several consecutive samples -- then costs a stat apiece and nothing more.

        Records, never text: a turn that answered through a tool still emits an
        `assistant` record but need not carry any of the user's words. Blocking
        (stat + read), so callers on the event loop hand it to a thread.
        """
        size = self.size(sdk_session_id)
        cached = self._counts.get(sdk_session_id)
        if cached is not None and cached[0] == size:
            return cached
        data = self.read(sdk_session_id) or b""
        # len(data), not `size`: the CLI may have appended between the stat and the
        # read, and the pair must describe one set of bytes.
        reading = (len(data), data.count(_ASSISTANT_RECORD))
        self._counts[sdk_session_id] = reading
        return reading

    def write(self, sdk_session_id: str, data: bytes) -> None:
        path = self.path(sdk_session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        # Hydration REPLACES the file, so append-only -- the assumption sample()'s
        # cache rests on -- does not hold across it.
        self._counts.pop(sdk_session_id, None)

    def delete(self, sdk_session_id: str) -> None:
        self.path(sdk_session_id).unlink(missing_ok=True)
        self._counts.pop(sdk_session_id, None)

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass
class Attachment:
    name: str
    path: str


class _HandlesTurns(Protocol):
    async def handle(self, channel: str, thread_ts: str, text: str) -> None: ...


def build_turn_text(text: str, attachments: Sequence[Attachment] = ()) -> str:
    """Append attachment metadata as `<attachment .../>` blocks the agent can
    read (a plain-text envelope, not a real file transfer -- v1's attachment
    handling is metadata-only, see the plan's self-review)."""
    if not attachments:
        return text
    blocks = "\n".join(f'<attachment name="{a.name}" path="{a.path}"/>' for a in attachments)
    return f"{text}\n\n{blocks}"


async def dispatch(
    manager: _HandlesTurns,
    *,
    channel: str,
    thread_ts: str,
    text: str,
    attachments: Sequence[Attachment] = (),
) -> None:
    turn_text = build_turn_text(text, attachments)
    await manager.handle(channel, thread_ts, turn_text)

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

# Anything shaped like the author envelope is stripped out of the human's own text
# before ours is prepended, so a turn carries exactly one and it is always the one
# the gateway wrote. Without this, typing the tag into a message is enough to claim
# somebody else's id. Slack ids are `[A-Z0-9]+`, but the pattern is deliberately
# looser than that: it strips the malformed lookalikes too.
_AUTHOR_TAG_RE = re.compile(r"<\s*slack-author\b[^>]*>", re.IGNORECASE)


@dataclass
class Attachment:
    name: str
    path: str


class _HandlesTurns(Protocol):
    async def handle(self, channel: str, thread_ts: str, text: str) -> None: ...


def build_turn_text(
    text: str,
    attachments: Sequence[Attachment] = (),
    author_id: str | None = None,
) -> str:
    """Wrap the human's message in the plain-text envelope the agent reads.

    `<slack-author id="U…"/>` names who wrote this message. Slack delivers the
    author on every event, but until this envelope existed the agent was only
    ever handed the message body, so it could not tell two people in a thread
    apart or answer "who is asking?" at all.

    It is context, never a gate: engagement, approver authorization and
    permission are decided in gateway code before dispatch is reached, and a
    persona that reads this id decides nothing security-relevant with it (see
    the trust boundary in CLAUDE.md).

    `<attachment .../>` blocks stay metadata-only, as before -- a plain-text
    envelope, not a real file transfer (see the plan's self-review).
    """
    body = _AUTHOR_TAG_RE.sub("", text) if author_id else text
    parts = []
    if author_id:
        parts.append(f'<slack-author id="{author_id}"/>')
    parts.append(body)
    if attachments:
        parts.append(
            "\n".join(f'<attachment name="{a.name}" path="{a.path}"/>' for a in attachments)
        )
    return "\n\n".join(parts)


async def dispatch(
    manager: _HandlesTurns,
    *,
    channel: str,
    thread_ts: str,
    text: str,
    attachments: Sequence[Attachment] = (),
    author_id: str | None = None,
) -> None:
    turn_text = build_turn_text(text, attachments, author_id)
    await manager.handle(channel, thread_ts, turn_text)

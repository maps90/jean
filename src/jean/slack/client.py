from __future__ import annotations

import contextlib
from typing import Any

from jean.slack.mrkdwn import chunk_text, md_to_mrkdwn


class SlackSurface:
    """ChatSurface adapter over a Slack `AsyncWebClient` (or any object with
    the same async method surface -- see tests for the fake used here)."""

    def __init__(self, web_client: Any) -> None:
        self._client = web_client

    async def reply(self, channel: str, thread_ts: str, text: str) -> str:
        mrkdwn = md_to_mrkdwn(text)
        first_ts: str | None = None
        for chunk in chunk_text(mrkdwn):
            resp = await self._client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=chunk
            )
            if first_ts is None:
                first_ts = resp["ts"]
        if first_ts is None:  # pragma: no cover -- chunk_text always returns >=1 chunk
            raise RuntimeError("reply() posted no chunks; chunk_text must yield at least one")
        return first_ts

    async def edit(self, channel: str, ts: str, text: str) -> None:
        await self._client.chat_update(channel=channel, ts=ts, text=md_to_mrkdwn(text))

    async def upload(
        self,
        channel: str,
        thread_ts: str,
        *,
        path: str | None = None,
        content: str | None = None,
        filename: str,
        title: str | None = None,
        comment: str | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {"channel": channel, "thread_ts": thread_ts, "filename": filename}
        if path is not None:
            kwargs["file"] = path
        if content is not None:
            kwargs["content"] = content
        if title is not None:
            kwargs["title"] = title
        if comment is not None:
            kwargs["initial_comment"] = comment
        await self._client.files_upload_v2(**kwargs)

    async def react(self, channel: str, ts: str, emoji: str) -> None:
        await self._client.reactions_add(channel=channel, name=emoji.strip(":"), timestamp=ts)

    async def unreact(self, channel: str, ts: str, emoji: str) -> None:
        await self._client.reactions_remove(channel=channel, name=emoji.strip(":"), timestamp=ts)

    async def set_status(self, channel: str, thread_ts: str, status: str) -> None:
        # Best-effort Slack nicety: the `assistant:write` scope may be absent,
        # or the surface may not support thread status at all -- swallow it.
        with contextlib.suppress(Exception):
            await self._client.assistant_threads_setStatus(
                channel_id=channel, thread_ts=thread_ts, status=status
            )

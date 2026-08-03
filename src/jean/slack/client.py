from __future__ import annotations

import contextlib
import re
from typing import Any

from slack_sdk.errors import SlackApiError

from jean.ports import ChatReadError
from jean.slack.mrkdwn import chunk_text, md_to_mrkdwn

# A Slack conversation id, which is passed through untouched. G/D are matched
# too even though reads are public-channel-only: letting them through means the
# agent gets Slack's own honest error rather than a guess made here.
_CHANNEL_ID = re.compile(r"^[CGD][A-Z0-9]+$")
_LIST_PAGE = 1000


def _read_error(exc: SlackApiError) -> ChatReadError:
    """Slack's error string, carried into a domain error so `slack/mcp.py` can
    report the real reason without importing slack_sdk."""
    response = getattr(exc, "response", None)
    code = "slack_error"
    if response is not None:
        # `response` is a dict on a hand-built error and a SlackResponse from a
        # real call; both carry .get, but neither is guaranteed by the type.
        with contextlib.suppress(AttributeError, TypeError):
            code = response.get("error") or code
    return ChatReadError(code, f"Slack refused the read: {code}")


class SlackSurface:
    """ChatSurface adapter over a Slack `AsyncWebClient` (or any object with
    the same async method surface -- see tests for the fake used here)."""

    def __init__(self, web_client: Any) -> None:
        self._client = web_client
        # name -> id, for this worker's lifetime. Channel ids are stable, so a
        # cold miss costs one paged scan per worker rather than one per read.
        self._channel_ids: dict[str, str] = {}

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

    async def resolve_channel(self, name_or_id: str) -> str:
        """`#sre-support` / `sre-support` / `C0123ABC` -> a channel id."""
        name = name_or_id.strip().lstrip("#")
        if _CHANNEL_ID.match(name):
            return name
        if name in self._channel_ids:
            return self._channel_ids[name]

        cursor: str | None = None
        while True:
            try:
                page = await self._client.conversations_list(
                    types="public_channel",
                    exclude_archived=True,
                    limit=_LIST_PAGE,
                    **({"cursor": cursor} if cursor else {}),
                )
            except SlackApiError as exc:
                raise _read_error(exc) from exc
            for channel in page.get("channels") or []:
                if channel.get("name") and channel.get("id"):
                    self._channel_ids[channel["name"]] = channel["id"]
            if name in self._channel_ids:
                return self._channel_ids[name]
            cursor = (page.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break

        raise ChatReadError(
            "channel_not_found",
            f"no public channel named {name_or_id!r} that this app can see -- "
            "check the name, and that the app is installed with channels:read",
        )

    async def set_status(self, channel: str, thread_ts: str, status: str) -> None:
        # Best-effort Slack nicety: the `assistant:write` scope may be absent,
        # or the surface may not support thread status at all -- swallow it.
        with contextlib.suppress(Exception):
            await self._client.assistant_threads_setStatus(
                channel_id=channel, thread_ts=thread_ts, status=status
            )

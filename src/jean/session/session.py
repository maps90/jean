from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jean.ports import ChatSurface, SessionStore


@dataclass
class RoutingContext:
    """Mutable per-turn routing the in-process MCP tools read via
    channel_of()/thread_of() closures (see slack/mcp.py) -- the SDK agent has
    no other way to know which Slack thread it is replying in."""

    channel: str = ""
    thread_ts: str = ""


class JeanSession:
    """One Slack thread's persistent claude-agent session.

    A client connects lazily on the first `run_turn` and is kept open across
    subsequent turns on *this* instance for efficiency; correctness never
    depends on that, though -- `sdk_session_id` is persisted to the
    SessionStore after every turn, so if this instance (or its cached client)
    is ever dropped, the next `run_turn` (here or on another worker) resumes
    from the stored id (the stateless-worker model).
    """

    def __init__(
        self,
        channel: str,
        thread_ts: str,
        *,
        store: SessionStore,
        chat: ChatSurface,
        routing: RoutingContext,
        options_factory: Callable[[str | None], Any],
        client_factory: Callable[..., Any],
    ) -> None:
        self._channel = channel
        self._thread_ts = thread_ts
        self._store = store
        self._chat = chat
        self._routing = routing
        self._options_factory = options_factory
        self._client_factory = client_factory
        self._client: Any | None = None

    async def run_turn(self, text: str) -> None:
        self._routing.channel = self._channel
        self._routing.thread_ts = self._thread_ts
        await self._chat.set_status(self._channel, self._thread_ts, "is thinking...")
        try:
            if self._client is None:
                row = await self._store.get_session(self._channel, self._thread_ts)
                resume = row.sdk_session_id if row else None
                options = self._options_factory(resume)
                self._client = self._client_factory(options=options)
                await self._client.__aenter__()

            await self._client.query(text)
            async for msg in self._client.receive_response():
                sid = getattr(msg, "session_id", None)
                if sid:
                    await self._store.upsert_session(
                        self._channel, self._thread_ts, sdk_session_id=sid
                    )
        except BaseException:
            # Never leave a poisoned, non-None, un-entered client around: if
            # client creation or any step of the turn raised, every later turn
            # on this thread would reuse that same broken client forever.
            # Best-effort tear down and drop it so the next run_turn rebuilds
            # fresh and resumes from the stored sdk_session_id, exactly like
            # the stateless-worker resume path.
            if self._client is not None:
                with contextlib.suppress(Exception):
                    await self._client.__aexit__(None, None, None)
                self._client = None
            raise
        finally:
            # "" clears the assistant thread status (see slack/client.py).
            await self._chat.set_status(self._channel, self._thread_ts, "")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None

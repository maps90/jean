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

    async def _open(self, resume: str | None) -> Any:
        options = self._options_factory(resume)
        client = self._client_factory(options=options)
        try:
            await client.__aenter__()
        except BaseException:
            with contextlib.suppress(Exception):
                await client.__aexit__(None, None, None)
            raise
        return client

    async def _connect(self) -> Any:
        """Connect a client, resuming the stored sdk_session_id when there is one.

        The stored id can outlive the transcript it names: the CLI writes each
        conversation to the *local* filesystem ($HOME/.claude/projects/<cwd>/
        <id>.jsonl) while jean persists only the id in Postgres, so a restarted
        pod -- or any other replica -- resumes an id it cannot see and the CLI
        exits 1 during startup, before the turn even begins. Rather than fail
        the user's message, reconnect without `resume`: the thread keeps
        working, having lost the agent's memory of its earlier turns (the next
        ResultMessage overwrites the unusable id in the store).

        A startup failure that is *not* about the resume id (bad --plugin-dir,
        bad auth) exits 1 the same way, so tell them apart by outcome rather
        than by parsing the CLI's stderr: if connecting without `resume` fails
        too, it was never the resume -- propagate, and leave the stored id
        alone.
        """
        row = await self._store.get_session(self._channel, self._thread_ts)
        resume = row.sdk_session_id if row else None
        if resume is None:
            return await self._open(None)
        try:
            return await self._open(resume)
        except Exception:
            client = await self._open(None)
        await self._chat.reply(
            self._channel,
            self._thread_ts,
            "_(I couldn't pick up where we left off in this thread — "
            "my memory of the earlier turns is gone. Starting fresh.)_",
        )
        return client

    async def run_turn(self, text: str) -> None:
        self._routing.channel = self._channel
        self._routing.thread_ts = self._thread_ts
        await self._chat.set_status(self._channel, self._thread_ts, "is thinking...")
        try:
            if self._client is None:
                self._client = await self._connect()

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

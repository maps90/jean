from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jean.ports import ChatSurface, SessionStore, TranscriptStore
from jean.session.transcript import LocalTranscripts

logger = logging.getLogger(__name__)


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

    The CLI keeps a thread's transcript on the *local* disk of whichever pod
    wrote it, though, so a cached client is not enough: this class also
    hydrates that transcript from the TranscriptStore before a cold connect,
    and archives it back after every turn, so any worker can pick the thread
    up. A `turn_seq` counter on the session row guards a cached client against
    silently going stale when another worker took a turn in between (see
    `run_turn`).
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
        transcripts: TranscriptStore,
        local: LocalTranscripts,
        max_transcript_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        self._channel = channel
        self._thread_ts = thread_ts
        self._store = store
        self._chat = chat
        self._routing = routing
        self._options_factory = options_factory
        self._client_factory = client_factory
        self._transcripts = transcripts
        self._local = local
        self._max_transcript_bytes = max_transcript_bytes
        self._client: Any | None = None
        self._seen_seq: int = 0  # turn_seq this instance's client is current with
        self._sid: str | None = None  # session id its transcript is stored under
        self._archived = False  # is the store's copy up to date with local disk?
        self._busy = False  # is a turn in flight? (the idle sweeper must not close us)

    @property
    def busy(self) -> bool:
        """True while a turn is in flight. The idle sweeper reads this: closing a
        session mid-turn tears down the client and deletes the .jsonl the CLI child
        still has open, so the turn would archive nothing and the thread would
        rewind. A turn parked on a human approval outlives the idle window
        (approval_ttl 30min > idle_minutes 15), so "idle" is not "not running"."""
        return self._busy

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
        exits 1 during startup, before the turn even begins. This worker may
        simply never have handled this thread before, though, so before
        attempting the resume we materialize Postgres's copy of the transcript
        onto our own disk -- that is the common case under N>1 workers, and it
        is what makes the resume succeed instead of silently starting fresh.

        Only if the transcript is genuinely gone (e.g. expired by retention,
        or this is the very first turn) does the resume fail. Rather than fail
        the user's message, reconnect without `resume`: the thread keeps
        working, having lost the agent's memory of its earlier turns (the next
        ResultMessage overwrites the unusable id in the store).

        A startup failure that is *not* about the resume id (bad --plugin-dir,
        bad auth) exits 1 the same way, so tell them apart by outcome rather
        than by parsing the CLI's stderr: if connecting without `resume` fails
        too, it was never the resume -- propagate, and leave the stored id
        alone.
        """
        committed_seq = self._seen_seq  # the turn_seq THIS instance last committed
        row = await self._store.get_session(self._channel, self._thread_ts)
        resume = row.sdk_session_id if row else None
        self._seen_seq = row.turn_seq if row else 0
        if resume is None:
            return await self._open(None)

        # ... unless OUR local file is the newer copy. `_archived is False` for
        # `_sid` means the store never took our last turn (a save() failed, or the
        # transcript was over the cap), so its blob is older and hydrating from it
        # would erase that turn. Only this instance's own bookkeeping can tell us
        # that, and it is trustworthy here: the staleness path in run_turn calls
        # close() before reconnecting, and close() deletes the local file whenever
        # it IS archived -- so a file that survives with `_archived False` is
        # genuinely ours.
        #
        # Ours, but only *newer* while we are still the thread's most recent
        # writer -- hence `turn_seq == committed_seq`. If the row has advanced past
        # the seq we committed, another worker took a turn in between: it hydrated
        # the store's copy (which lacks our un-archived turn), answered on top of
        # it, and archived. Our file is then not ahead of the store, just a
        # divergent branch, and resuming it would archive a history missing THEIR
        # turn over theirs -- destroying an answer the user has already seen. The
        # store's turn_seq is the authority: once it moves past us it is canonical,
        # so we hydrate and swallow the loss of our own un-archived turn. Converging
        # on the store costs one turn; the alternative is a lost update that can
        # ping-pong between workers forever.
        local_is_newer = (
            self._sid == resume
            and not self._archived
            and self._seen_seq == committed_seq
            and self._local.path(resume).exists()
        )
        if not local_is_newer:
            data = await self._transcripts.load(self._channel, self._thread_ts, resume)
            if data is not None:
                self._local.write(resume, data)
        try:
            return await self._open(resume)
        except Exception:
            client = await self._open(None)
            # Only now do we know the resume id itself was refused: a failure that
            # was never about the resume (bad auth, bad --plugin-dir) would have
            # failed here too and propagated, leaving the file untouched. So the
            # file for THAT id is dead weight -- this turn gets a new session id
            # and nothing will ever reference the old one again. Delete it whether
            # we hydrated it from the store or it was our own un-archived copy;
            # the CLI has told us that copy cannot be resumed, and leaving it
            # orphans it on this pod's disk forever.
            self._local.delete(resume)
        await self._chat.reply(
            self._channel,
            self._thread_ts,
            "_(I couldn't pick up where we left off in this thread — "
            "my memory of the earlier turns is gone. Starting fresh.)_",
        )
        return client

    async def _archive(self, sdk_session_id: str) -> None:
        """Copy this pod's transcript into the store so any worker can resume it.

        `_archived` means "the store's copy is up to date with our local file for
        `_sid`", and close() reads it to decide whether deleting that local file is
        safe. It is NOT cleared here -- run_turn clears it before query(), which is
        the moment it actually stops being true (see there). Every path out of this
        method that does not reach a successful save() therefore leaves it False.
        """
        self._sid = sdk_session_id
        data = self._local.read(sdk_session_id)
        if data is None:
            # The CLI wrote its transcript somewhere we are not looking: a cli_home
            # or cwd that does not match the CLI's real project dir turns this whole
            # mechanism into a silent no-op (nothing archived, every cold worker
            # starts fresh). This log line is the only signal that would catch it.
            logger.warning(
                "no transcript at %s for %s/%s -- nothing to archive; this thread "
                "will not resume on another worker (is cli_home/cwd correct?)",
                self._local.path(sdk_session_id),
                self._channel,
                self._thread_ts,
            )
            return
        if len(data) > self._max_transcript_bytes:
            logger.warning(
                "transcript for %s/%s is %d bytes (> max %d); not archiving -- this "
                "thread will not resume on another worker",
                self._channel,
                self._thread_ts,
                len(data),
                self._max_transcript_bytes,
            )
            return
        try:
            await self._transcripts.save(self._channel, self._thread_ts, sdk_session_id, data)
            self._archived = True
        except Exception:
            # The turn already succeeded and the user has their answer; failing it
            # now would help no one. Log loudly and keep the local file as the only
            # copy (close() will not delete an unarchived transcript).
            logger.exception(
                "failed to archive transcript for %s/%s", self._channel, self._thread_ts
            )

    async def run_turn(self, text: str) -> None:
        self._routing.channel = self._channel
        self._routing.thread_ts = self._thread_ts
        self._busy = True
        await self._chat.set_status(self._channel, self._thread_ts, "is thinking...")
        try:
            if self._client is not None:
                # Another worker may have taken a turn on this thread since we
                # cached this client. A cached client never re-reads the store,
                # so it would answer from a history missing that turn and
                # archive over it. The stored turn_seq is how we notice.
                row = await self._store.get_session(self._channel, self._thread_ts)
                if row is None or row.turn_seq != self._seen_seq:
                    await self.close()

            if self._client is None:
                self._client = await self._connect()

            sid: str | None = None
            # Clear `_archived` HERE, not in _archive(): the flag means "the store's
            # copy is up to date with our local file", and query() is the exact
            # moment that stops being true -- the CLI child appends this turn to the
            # local .jsonl while it answers. Clearing it only in _archive() would
            # leave a `True` from the PREVIOUS turn standing over an already-newer
            # local file for the whole turn, so any failure in between (a dropped
            # connection in bump_turn, say) would let close() delete the only copy
            # of a turn the user has already seen. A turn that fails BEFORE the CLI
            # writes anything merely leaves the flag False over a local file that is
            # still identical to the store's -- harmless: we re-archive next turn.
            self._archived = False
            await self._client.query(text)
            async for msg in self._client.receive_response():
                got = getattr(msg, "session_id", None)
                if got:
                    sid = got
                    await self._store.upsert_session(
                        self._channel, self._thread_ts, sdk_session_id=sid
                    )
            if sid is not None:
                # Bump BEFORE archiving. The order is load-bearing, not tidiness:
                # these are two separate statements and a connection can drop
                # between them, and the two orders fail in opposite directions.
                #
                #   archive-then-bump: save() lands, bump_turn() fails -> the store
                #     holds the NEW transcript under an OLD turn_seq. Another
                #     worker's cached client still matches that turn_seq, so it
                #     believes it is current, answers from a history missing this
                #     turn, and archives that over the good transcript. Two
                #     divergent histories: corruption.
                #
                #   bump-then-archive (this): bump_turn() lands, save() fails ->
                #     the store holds an OLD transcript under a NEW turn_seq. Every
                #     other worker notices the change, drops its cached client and
                #     re-hydrates a coherent -- if one turn older -- history, while
                #     this worker keeps the newer local file (close() never deletes
                #     an un-archived one) and heals the store on its next turn.
                #     At worst one turn is lost.
                #
                # Losing a turn is recoverable; a corrupted thread is not.
                self._seen_seq = await self._store.bump_turn(self._channel, self._thread_ts)
                await self._archive(sid)
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
            self._busy = False
            # "" clears the assistant thread status (see slack/client.py).
            await self._chat.set_status(self._channel, self._thread_ts, "")

    async def close(self) -> None:
        if self._client is not None:
            # We are DISCARDING this client (idle sweep, or run_turn's staleness
            # branch mid-turn), so a failing teardown has nothing left to protect:
            # letting it escape would leave `_client` non-None -- poisoning every
            # later turn -- and, on the staleness path, fail the user's turn because
            # tearing down a client we no longer wanted went wrong.
            with contextlib.suppress(Exception):
                await self._client.__aexit__(None, None, None)
            self._client = None
        # Postgres is the durable copy, so a pod need not hoard transcripts for
        # threads it is no longer serving -- but never delete one the store failed
        # to take.
        if self._archived and self._sid is not None:
            self._local.delete(self._sid)
            self._archived = False

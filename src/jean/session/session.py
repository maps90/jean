from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jean.ports import ChatSurface, SessionRow, SessionStore, TranscriptStore
from jean.session.transcript import LocalTranscripts

logger = logging.getLogger(__name__)

# session/ is domain code and must not import claude_agent_sdk (CLAUDE.md's layering
# rule), so run_turn matches the SDK's AssistantMessage structurally, by class name,
# rather than importing the real class and using isinstance. This string is the whole
# of that contract: if claude_agent_sdk ever renames the class, matching against a
# hardcoded literal would silently stop counting -- collapsing the settle target to
# baseline+0 and returning before the CLI finishes writing the turn (the exact
# data-loss bug this module exists to prevent). tests/test_session.py pins this
# constant against the real SDK class's `__name__`, so a rename fails that test
# loudly instead. `_settle`'s `count_reliable` flag is the runtime backstop for the
# same failure: if a turn streams zero matches despite otherwise succeeding, it falls
# back to a conservative quiet-only wait instead of trusting target == baseline.
ASSISTANT_MESSAGE_CLASS_NAME = "AssistantMessage"


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
        options_factory: Callable[[str | None, str | None], Any],
        client_factory: Callable[..., Any],
        transcripts: TranscriptStore,
        local: LocalTranscripts,
        max_transcript_bytes: int,  # no default: Settings.transcript_max_mb owns the number
        # The settle wait (see _settle). Defaults kept only so a test can build a
        # JeanSession without caring; production wires all three from Settings.
        settle_timeout: float = 10.0,
        settle_interval: float = 0.1,
        settle_quiet: float = 1.0,
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
        self._settle_timeout = settle_timeout
        self._settle_interval = settle_interval
        self._settle_quiet = settle_quiet
        self._client: Any | None = None
        # The permission mode the cached client was opened with; the SDK fixes
        # it at connect, so a later /mode only lands on a fresh client.
        self._mode: str | None = None
        self._seen_seq: int = 0  # turn_seq this instance's client is current with
        self._sid: str | None = None  # session id its transcript is stored under
        self._live_sid: str | None = None  # id of the .jsonl the OPEN client writes to
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

    async def _open(self, resume: str | None, permission_mode: str | None) -> Any:
        options = self._options_factory(resume, permission_mode)
        client = self._client_factory(options=options)
        try:
            await client.__aenter__()
        except BaseException:
            with contextlib.suppress(Exception):
                await client.__aexit__(None, None, None)
            raise
        self._mode = permission_mode
        return client

    async def _connect(self, row: SessionRow | None) -> Any:
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

        `row` is read once by run_turn and passed in (rather than re-fetched
        here) so the two things a connect depends on -- the id/transcript to
        resume and the thread's permission_mode, which the SDK bakes in at
        connect and cannot change later -- come from the SAME snapshot as the
        staleness checks that decided to call us.
        """
        committed_seq = self._seen_seq  # the turn_seq THIS instance last committed
        resume = row.sdk_session_id if row else None
        mode = row.permission_mode if row else None
        self._seen_seq = row.turn_seq if row else 0
        if resume is None:
            self._live_sid = None  # a fresh session writes a .jsonl we cannot name yet
            return await self._open(None, mode)

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
            client = await self._open(resume, mode)
        except Exception:
            client = await self._open(None, mode)
            # Only now do we know the resume id itself was refused: a failure that
            # was never about the resume (bad auth, bad --plugin-dir) would have
            # failed here too and propagated, leaving the file untouched. So the
            # file for THAT id is dead weight -- this turn gets a new session id
            # and nothing will ever reference the old one again. Delete it whether
            # we hydrated it from the store or it was our own un-archived copy;
            # the CLI has told us that copy cannot be resumed, and leaving it
            # orphans it on this pod's disk forever.
            self._local.delete(resume)
            self._live_sid = None  # a fresh session: a new .jsonl under a new id
        else:
            # The resumed transcript is the file this client appends to, so run_turn's
            # settle wait measures its growth against the records already in it.
            self._live_sid = resume
            return client
        await self._chat.reply(
            self._channel,
            self._thread_ts,
            "_(I couldn't pick up where we left off in this thread — "
            "my memory of the earlier turns is gone. Starting fresh.)_",
        )
        return client

    async def _settle(self, sdk_session_id: str, target: int, *, count_reliable: bool) -> None:
        """Wait for the CLI to finish writing this turn to its .jsonl.

        The CLI's transcript writes are write-behind: when `receive_response()`
        returns, the user has their answer but the `assistant` record carrying it is
        typically NOT on disk yet (measured against the real CLI: it catches up on
        its own ~0.5s later, without the client being closed). Archiving at that
        moment persists a transcript whose last turn has no answer in it -- and it is
        silent: a cold worker hydrates that copy, the CLI sees a turn left hanging,
        injects a "Continue from where you left off." turn of its own, and the real
        answer is gone from the durable copy forever.

        Waiting for the count to merely RISE does not fix that, because a turn is not
        one `assistant` record. A real jean turn is a TOOL turn -- persona/identity.py
        mandates that every visible reply goes out through `mcp__jean_slack__reply` --
        and the CLI writes an `assistant` record per assistant message: thinking, each
        tool_use, then the final text. It flushes the early ones MID-TURN, so by the
        time we get here the count is already above any baseline, satisfied by a
        tool_use whose answer is still unwritten. (Worse: land in the gap between a
        tool_use and its tool_result and the archived history ends on an unresolved
        tool call.) So wait for an EXACT `target`: the records already on disk before
        query() plus the AssistantMessages the SDK streamed for this turn -- one per
        record the CLI owes the file (see run_turn).

        Then keep waiting until the file goes quiet, because `system` records trail
        the final `assistant` one by ~0.1s. Quiet means several CONSECUTIVE unchanged
        readings spanning `settle_quiet`: a single unchanged 100ms sample proves
        nothing against a writer that pauses for longer than that between records.

        Bounded: if the timeout expires we archive what is there anyway (an
        incomplete transcript still beats none) and say so loudly. The user already
        has their answer; the turn is never failed over this.

        `count_reliable=False` is run_turn's signal that it streamed zero
        AssistantMessages for a turn that otherwise succeeded -- the structural
        class-name match (see `ASSISTANT_MESSAGE_CLASS_NAME`) found nothing to count,
        most likely because claude_agent_sdk renamed the class out from under it. A
        completed turn always produces at least one `assistant` record, so `target`
        (baseline + 0) is not trustworthy here: trusting it would satisfy `records >=
        target` on the baseline alone and return before the CLI has written anything
        of THIS turn -- the original data-loss bug, resurrected, with a green test
        suite. So we warn loudly and drop the target check entirely, falling back to
        the conservative rule instead: wait for the file to sit quiet for the full
        window regardless of how many records that is. Slower, but it cannot
        silently truncate.
        """
        if not self._local.path(sdk_session_id).exists():
            return  # nothing on disk to settle -- _archive logs that as its own problem
        if not count_reliable:
            logger.warning(
                "could not count assistant messages for this turn on %s/%s (streamed "
                "0 while the turn otherwise succeeded) -- jean cannot know when the "
                "transcript is complete; likely cause: claude_agent_sdk renamed "
                "AssistantMessage, breaking session.py's structural class-name match. "
                "Falling back to waiting for the transcript to go quiet before "
                "archiving.",
                self._channel,
                self._thread_ts,
            )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settle_timeout
        quiet_needed = max(1, math.ceil(self._settle_quiet / self._settle_interval))
        last_size = -1  # no size seen yet, so the first reading can never look "unchanged"
        quiet = 0
        while True:
            # In a thread: this stats and (when the file has grown) re-reads a
            # transcript that may be tens of MB, and the event loop is also serving
            # every other thread's turn on this worker and the approval LISTEN wakeup.
            size, records = await asyncio.to_thread(self._local.sample, sdk_session_id)
            quiet = quiet + 1 if size == last_size else 0
            target_met = records >= target if count_reliable else True
            if target_met and quiet >= quiet_needed:
                return
            if loop.time() >= deadline:
                logger.warning(
                    "transcript for %s/%s did not settle within %.1fs (%d assistant "
                    "records on disk, expected %d) -- archiving it anyway, but it may be "
                    "missing this last turn; a worker that resumes it would lose the answer",
                    self._channel,
                    self._thread_ts,
                    self._settle_timeout,
                    records,
                    target,
                )
                return
            last_size = size
            await asyncio.sleep(self._settle_interval)

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
            # One read of the row serves both reasons a cached client can be wrong.
            row = await self._store.get_session(self._channel, self._thread_ts)
            if self._client is not None:
                # `/mode` may have run since this client was opened -- on this worker
                # or, in the stateless-worker model, on any other one. permission_mode
                # is baked into the client at connect, so honour a change by dropping
                # the cached client; the reconnect below resumes the same sdk session.
                mode_changed = (row.permission_mode if row else None) != self._mode
                # Another worker may also have taken a turn on this thread since we
                # cached this client. A cached client never re-reads the store, so it
                # would answer from a history missing that turn and archive over it.
                # The stored turn_seq is how we notice; _connect then re-hydrates.
                stale = row is None or row.turn_seq != self._seen_seq
                if mode_changed or stale:
                    with contextlib.suppress(Exception):
                        await self.close()
            if self._client is None:
                self._client = await self._connect(row)

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
            # How many `assistant` records the transcript holds BEFORE this turn: 0 on
            # a fresh session (no file yet), the hydrated count on a resumed one. This
            # turn's records land ON TOP of these, so it is what _settle's target counts
            # up from -- and it is counted against the file `baseline_sid` names, which
            # is why we hold on to that id rather than re-reading `_live_sid` later (the
            # receive loop below overwrites it).
            baseline_sid = self._live_sid
            baseline = (
                (await asyncio.to_thread(self._local.sample, baseline_sid))[1]
                if baseline_sid
                else 0
            )
            assistant_msgs = 0
            await self._client.query(text)
            async for msg in self._client.receive_response():
                # The SDK streams one AssistantMessage per `assistant` record the CLI
                # writes -- so counting them here tells _settle EXACTLY how many records
                # this turn still owes the .jsonl, instead of guessing. Matched by class
                # NAME rather than isinstance: session/ is domain code and must not
                # import claude_agent_sdk (CLAUDE.md's layering rule) -- the SDK reaches
                # this class only as an injected client_factory, and its messages are
                # duck-typed exactly like `session_id` is just below. See
                # ASSISTANT_MESSAGE_CLASS_NAME for what pins this string against the
                # real SDK class, and _settle's `count_reliable` for what happens at
                # runtime if it ever drifts.
                if type(msg).__name__ == ASSISTANT_MESSAGE_CLASS_NAME:
                    assistant_msgs += 1
                got = getattr(msg, "session_id", None)
                if got:
                    sid = got
                    self._live_sid = sid  # now we can name the file the CLI is writing
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
                # ... but archive nothing until the CLI has actually written this turn:
                # its .jsonl lags the response we have just finished streaming (_settle).
                #
                # The baseline only counts toward the target if it was counted against
                # THIS turn's file. If the id moved (the resume was refused, so the turn
                # ran in a fresh session under a new id), the baseline describes a
                # different .jsonl and is almost certainly too high for this one: keeping
                # it would set an unreachable target, burn the whole settle timeout on
                # every turn, and warn about a truncation that never happened.
                if baseline_sid is not None and baseline_sid != sid:
                    logger.warning(
                        "session id changed mid-turn for %s/%s (%s -> %s); counting this "
                        "turn's transcript from zero",
                        self._channel,
                        self._thread_ts,
                        baseline_sid,
                        sid,
                    )
                    baseline = 0
                # A completed turn always writes at least one `assistant` record
                # (persona/identity.py mandates every reply goes out through a tool
                # call, so a real turn is thinking/tool_use/text at minimum), so
                # `assistant_msgs == 0` here means the structural match above found
                # nothing to count, not that the turn produced nothing. Trusting the
                # target in that case would let _settle return the moment the
                # baseline alone satisfies it -- see `_settle`'s `count_reliable`.
                count_reliable = assistant_msgs > 0
                await self._settle(sid, baseline + assistant_msgs, count_reliable=count_reliable)
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

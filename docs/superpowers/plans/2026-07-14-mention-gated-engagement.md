# Mention-Gated Engagement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace jean's thread-wide `engaged` boolean with a single conversation
partner per thread, so a message from anyone who is not that partner (and who did
not @-mention her) costs no agent turn, no tokens, and no place in the per-thread
lock queue.

**Architecture:** `sessions.engaged boolean` becomes `sessions.engaged_with text`
(nullable Slack user id). The pure decision function `gateway/engagement.py::decide()`
takes the current partner and returns the *resulting* partner, so "set", "clear" and
"unchanged" are all expressible without a sentinel. `gateway/app.py` writes to the
store only when the partner actually changed, which is what keeps an ignored message
free.

**Tech Stack:** Python 3.11+, asyncpg, pytest (+pytest-asyncio, `asyncio_mode="auto"`),
ruff.

**Spec:** `docs/superpowers/specs/2026-07-14-mention-gated-engagement-design.md`

## Global Constraints

- Work in the existing worktree `.claude/worktrees/mention-gated-engagement`
  (branch `mention-gated-engagement`). Do not touch the primary checkout.
- `from __future__ import annotations` at the top of every module; modern type
  hints (`str | None`).
- **Do NOT add AI co-author trailers to commits in this repo.**
- Run `./scripts/verify.sh` (ruff check + ruff format-check + pytest) before every
  commit. Test output must be pristine — no stray warnings.
- Layering rule: `gateway/` must not import `asyncpg` or `slack_*`. It talks to the
  `SessionStore` port only.
- **The test suite must be green at every commit.** The task order below exists
  precisely for this: the new `set_partner`/`get_partner` API is added first
  (Task 1) and the old `engaged` API is deleted only once its last caller is gone
  (Task 3). Do not reorder.

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `src/jean/ports.py` | `SessionStore` protocol + `SessionRow` | 1, 3 |
| `src/jean/db/memory.py` | in-memory adapter | 1, 3 |
| `src/jean/db/postgres.py` | asyncpg adapter + schema | 1, 3 |
| `tests/store_behavior.py` | shared assertions both adapters satisfy | 1, 3 |
| `src/jean/gateway/engagement.py` | the pure decision (no I/O) | 2 |
| `src/jean/gateway/app.py` | wiring: read partner, decide, write-if-changed | 2 |
| `tests/test_engagement.py` | decision table | 2 |
| `tests/test_gateway_app.py` | gateway behavior against the in-memory store | 2 |
| `src/jean/persona/identity.py` | the prompt's Engagement paragraph | 4 |

---

### Task 1: Store the partner (additive — old `engaged` API untouched)

Add `engaged_with` alongside `engaged`. Nothing reads it yet, so the suite stays
green.

**Files:**
- Modify: `src/jean/ports.py:9-16` (`SessionRow`), `:45-59` (`SessionStore`)
- Modify: `src/jean/db/memory.py:41-89`
- Modify: `src/jean/db/postgres.py:11-42` (schema), `:77-130`
- Test: `tests/store_behavior.py` (new assertion fn, called by the two existing
  adapter tests)

**Interfaces:**
- Consumes: nothing.
- Produces: `SessionStore.set_partner(channel: str, thread_ts: str, user_id: str | None) -> None`,
  `SessionStore.get_partner(channel: str, thread_ts: str) -> str | None`,
  `SessionRow.engaged_with: str | None`. Task 2 depends on exactly these names.

- [ ] **Step 1: Write the failing test**

Add to `tests/store_behavior.py`:

```python
async def assert_partner_roundtrip(store) -> None:
    """One conversation partner per thread. `None` means nobody -- and clearing
    to `None` must be distinguishable from 'leave it alone', which is why this
    is a dedicated setter rather than a field on upsert_session()."""
    channel, thread_ts = "C9", "999.111"
    assert await store.get_partner(channel, thread_ts) is None

    await store.set_partner(channel, thread_ts, "U11111")
    assert await store.get_partner(channel, thread_ts) == "U11111"
    row = await store.get_session(channel, thread_ts)
    assert row is not None
    assert row.engaged_with == "U11111"

    # A second mention hands the conversation to someone else.
    await store.set_partner(channel, thread_ts, "U22222")
    assert await store.get_partner(channel, thread_ts) == "U22222"

    # Clearing to None is a real, storable state, not a no-op.
    await store.set_partner(channel, thread_ts, None)
    assert await store.get_partner(channel, thread_ts) is None

    # The partner must survive an unrelated update that doesn't mention it.
    await store.set_partner(channel, thread_ts, "U11111")
    await store.upsert_session(channel, thread_ts, sdk_session_id="sdk-xyz", touch=False)
    assert await store.get_partner(channel, thread_ts) == "U11111"
```

Wire it into both adapter tests. In `tests/test_memory_store.py` and
`tests/test_postgres_store.py`, find the existing test that calls
`assert_session_roundtrip_and_engagement(store)` and add a sibling test next to it,
matching each file's existing fixture style (memory constructs `MemoryStore()`
directly; postgres uses its `store` fixture, which is skipped unless
`JEAN_TEST_DATABASE_URL` is set):

```python
async def test_partner_roundtrip(store):          # postgres: keep the fixture arg
    await assert_partner_roundtrip(store)
```

and import `assert_partner_roundtrip` alongside the existing
`assert_session_roundtrip_and_engagement` import in both files.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd .claude/worktrees/mention-gated-engagement && uv run pytest tests/test_memory_store.py -k partner -v`
Expected: FAIL — `AttributeError: 'MemoryStore' object has no attribute 'get_partner'`

- [ ] **Step 3: Add `engaged_with` to the port**

In `src/jean/ports.py`, add the field to `SessionRow` (keep `engaged` for now):

```python
@dataclass
class SessionRow:
    channel: str
    thread_ts: str
    sdk_session_id: str | None
    permission_mode: str | None
    engaged: bool
    last_active_at: float
    turn_seq: int = 0
    engaged_with: str | None = None
```

and add the two methods to the `SessionStore` protocol, below `is_engaged`:

```python
    async def set_partner(self, channel: str, thread_ts: str, user_id: str | None) -> None: ...
    async def get_partner(self, channel: str, thread_ts: str) -> str | None: ...
```

- [ ] **Step 4: Implement in `MemoryStore`**

In `src/jean/db/memory.py::get_session`, add `engaged_with=row.engaged_with,` to the
returned `SessionRow(...)`.

In `upsert_session`, preserve it across the rebuild — the method constructs a fresh
`SessionRow` every call, so an unlisted field would be silently dropped. Add to the
`SessionRow(...)` literal:

```python
            engaged_with=existing.engaged_with if existing else None,
```

Then add the two methods after `is_engaged`:

```python
    async def set_partner(self, channel: str, thread_ts: str, user_id: str | None) -> None:
        # Not routed through upsert_session: `None` there means "leave unchanged",
        # but here it means "clear the partner" -- a real state we must be able to
        # write.
        key = (channel, thread_ts)
        if key not in self._sessions:
            await self.upsert_session(channel, thread_ts, touch=False)
        self._sessions[key].engaged_with = user_id

    async def get_partner(self, channel: str, thread_ts: str) -> str | None:
        row = self._sessions.get((channel, thread_ts))
        return row.engaged_with if row else None
```

- [ ] **Step 5: Implement in `PostgresStore`**

In `src/jean/db/postgres.py`, append to `_SCHEMA` (after the `turn_seq` ALTER, before
the `transcripts` STORAGE line):

```sql
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS engaged_with text;
```

In `get_session`, add `engaged_with=r["engaged_with"],` to the `SessionRow(...)`.

Add the two methods after `is_engaged`:

```python
    async def set_partner(self, channel: str, thread_ts: str, user_id: str | None) -> None:
        # A plain assignment, not COALESCE: $3 = NULL must clear the partner, not
        # mean "keep what's there".
        await self._pool.execute(
            """INSERT INTO sessions(channel,thread_ts,engaged_with,last_active_at)
               VALUES($1,$2,$3,0)
               ON CONFLICT(channel,thread_ts) DO UPDATE SET engaged_with=$3""",
            channel,
            thread_ts,
            user_id,
        )

    async def get_partner(self, channel: str, thread_ts: str) -> str | None:
        return await self._pool.fetchval(
            "SELECT engaged_with FROM sessions WHERE channel=$1 AND thread_ts=$2",
            channel,
            thread_ts,
        )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `./scripts/verify.sh`
Expected: PASS. (The postgres partner test is skipped unless `JEAN_TEST_DATABASE_URL`
is set — that's fine, CI runs it.)

- [ ] **Step 7: Commit**

```bash
git add src/jean/ports.py src/jean/db/memory.py src/jean/db/postgres.py tests/store_behavior.py tests/test_memory_store.py tests/test_postgres_store.py
git commit -m "feat(store): store a thread's conversation partner (engaged_with)"
```

---

### Task 2: Decide by partner, and only write when it changes

**Files:**
- Modify: `src/jean/gateway/engagement.py:16-57`
- Modify: `src/jean/gateway/app.py:71-103`
- Test: `tests/test_engagement.py`, `tests/test_gateway_app.py`

**Interfaces:**
- Consumes: `SessionStore.get_partner`/`set_partner` from Task 1.
- Produces: `Decision(handle: bool, partner: str | None)` and
  `decide(*, bot_id, channel, thread_ts, text, is_dm, soul, partner, author_id=None)`.
  `Decision.partner` is always the **resulting** partner — for the unchanged cases
  it is the same value that was passed in.

- [ ] **Step 1: Write the failing tests**

Rewrite the `decide()` cases in `tests/test_engagement.py`. Keep the three
`mentions_in` tests and the `_soul` helper exactly as they are; replace every test
below them with:

```python
def test_blocked_author_is_dropped():
    soul = _soul(blocked_users=["U66666"])
    d = decide(
        bot_id="UBOT", channel="C1", thread_ts="1.0", text="hello",
        is_dm=False, soul=soul, partner="U66666", author_id="U66666",
    )
    assert d.handle is False
    assert d.partner == "U66666"  # unchanged -- blocking is not disengaging


def test_dm_always_handles_and_takes_the_author_as_partner():
    soul = _soul()
    d = decide(
        bot_id="UBOT", channel="D1", thread_ts="1.0", text="hello",
        is_dm=True, soul=soul, partner=None, author_id="U11111",
    )
    assert d.handle is True
    assert d.partner == "U11111"


def test_mention_of_bot_handles_and_makes_the_mentioner_the_partner():
    soul = _soul()
    d = decide(
        bot_id="UBOT", channel="C1", thread_ts="1.0", text="hey <@UBOT> help me",
        is_dm=False, soul=soul, partner=None, author_id="U11111",
    )
    assert d.handle is True
    assert d.partner == "U11111"


def test_mention_by_a_second_person_takes_over_the_conversation():
    soul = _soul()
    d = decide(
        bot_id="UBOT", channel="C1", thread_ts="1.0", text="<@UBOT> actually, over here",
        is_dm=False, soul=soul, partner="U11111", author_id="U22222",
    )
    assert d.handle is True
    assert d.partner == "U22222"


def test_mention_of_someone_else_clears_the_partner_and_does_not_handle():
    soul = _soul()
    d = decide(
        bot_id="UBOT", channel="C1", thread_ts="1.0", text="hey <@U22222> can you help",
        is_dm=False, soul=soul, partner="U11111", author_id="U11111",
    )
    assert d.handle is False
    assert d.partner is None


def test_plain_follow_up_from_the_partner_is_handled():
    soul = _soul()
    d = decide(
        bot_id="UBOT", channel="C1", thread_ts="1.0", text="ok now restart it",
        is_dm=False, soul=soul, partner="U11111", author_id="U11111",
    )
    assert d.handle is True
    assert d.partner == "U11111"


def test_plain_message_from_a_non_partner_is_ignored():
    """The whole point of the feature: Budi's aside to Dimas costs no turn."""
    soul = _soul()
    d = decide(
        bot_id="UBOT", channel="C1", thread_ts="1.0", text="that started after friday's deploy",
        is_dm=False, soul=soul, partner="U11111", author_id="U22222",
    )
    assert d.handle is False
    assert d.partner == "U11111"  # unchanged: Dimas is still the partner


def test_plain_message_with_no_partner_is_ignored():
    soul = _soul()
    d = decide(
        bot_id="UBOT", channel="C1", thread_ts="1.0", text="random chatter",
        is_dm=False, soul=soul, partner=None, author_id="U11111",
    )
    assert d.handle is False
    assert d.partner is None


def test_mention_of_bot_takes_priority_over_mention_of_others():
    soul = _soul()
    d = decide(
        bot_id="UBOT", channel="C1", thread_ts="1.0", text="hey <@UBOT> and <@U22222>",
        is_dm=False, soul=soul, partner=None, author_id="U11111",
    )
    assert d.handle is True
    assert d.partner == "U11111"


def test_mention_with_unknown_author_stores_no_partner():
    """Slack gave us no author id -- handle the mention, but store no partner, so
    the thread falls back to strict mention-only rather than to a wrong partner."""
    soul = _soul()
    d = decide(
        bot_id="UBOT", channel="C1", thread_ts="1.0", text="<@UBOT> hi",
        is_dm=False, soul=soul, partner="U11111", author_id=None,
    )
    assert d.handle is True
    assert d.partner is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_engagement.py -v`
Expected: FAIL — `TypeError: decide() got an unexpected keyword argument 'partner'`

- [ ] **Step 3: Rewrite `decide()`**

Replace `Decision` and `decide` in `src/jean/gateway/engagement.py` (keep the imports
and `mentions_in` as they are):

```python
@dataclass
class Decision:
    handle: bool
    partner: str | None  # the thread's partner AFTER this message


def decide(
    *,
    bot_id: str,
    channel: str,
    thread_ts: str,
    text: str,
    is_dm: bool,
    soul: SoulData,
    partner: str | None,
    author_id: str | None = None,
) -> Decision:
    """Pure engagement/authorization decision -- no I/O, no gates. `partner` is the
    thread's current conversation partner, read from the SessionStore by the caller
    (gateway/app.py) so this stays synchronous and trivially testable.

    `Decision.partner` is always the *resulting* partner, never a "leave it alone"
    sentinel: the unchanged cases just hand `partner` back. The caller compares it
    with what it read and writes only on a change -- that's what keeps an ignored
    message free of a database write.

    channel/thread_ts are accepted for future channel-scoping (e.g. allowed_channels)
    but unused today.
    """
    del channel, thread_ts  # reserved for future channel-scoping

    if author_id is not None and author_id in soul.blocked_users:
        return Decision(handle=False, partner=partner)

    if is_dm:
        return Decision(handle=True, partner=author_id)

    mentions = mentions_in(text)
    if bot_id in mentions:
        # Most recent mention wins: whoever addresses her is who she's talking to.
        # An unknown author leaves no partner, so the thread falls back to strict
        # mention-only rather than to a stale or wrong one.
        return Decision(handle=True, partner=author_id)

    if mentions:
        # Someone else was addressed in this thread -- jean steps back.
        return Decision(handle=False, partner=None)

    if author_id is not None and author_id == partner:
        # The partner's plain follow-up: no re-@mention needed.
        return Decision(handle=True, partner=partner)

    # Anyone else's plain message. This is the line the whole feature exists for:
    # no turn, no tokens, and no place in the thread's lock queue.
    return Decision(handle=False, partner=partner)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_engagement.py -v`
Expected: PASS (10 decide tests + 3 mentions_in tests)

- [ ] **Step 5: Write the failing gateway tests**

In `tests/test_gateway_app.py`, replace the engagement-related tests (keep
`FakeManager`, `FakeGate`, `_soul`, `_gateway`, and the `on_action`/`on_command`/
`ephemeral_for` tests untouched):

```python
async def test_on_mention_sets_partner_and_dispatches():
    gw, store, manager, _gate = _gateway()

    await gw.on_mention(
        channel="C1", thread_ts="111.0", text="hey <@UBOT> help me", author_id="U11111"
    )

    assert await store.get_partner("C1", "111.0") == "U11111"
    assert manager.calls == [("C1", "111.0", "hey <@UBOT> help me")]


async def test_on_mention_blocked_author_is_ignored():
    gw, store, manager, _gate = _gateway(soul=_soul(blocked_users=["U66666"]))

    await gw.on_mention(
        channel="C1", thread_ts="111.0", text="hey <@UBOT> help me", author_id="U66666"
    )

    assert await store.get_partner("C1", "111.0") is None
    assert manager.calls == []


async def test_on_message_skips_bot_mention_to_avoid_double_dispatch():
    """Slack delivers a channel @mention as BOTH app_mention and message
    events; on_mention owns bot-mentions, so on_message must no-op here."""
    gw, store, manager, _gate = _gateway()

    await gw.on_message("C1", "111.0", "hey <@UBOT> help me", "U11111", False)

    assert manager.calls == []
    assert await store.get_partner("C1", "111.0") is None


async def test_partner_follow_up_handled_but_a_bystander_is_ignored():
    """The end-to-end shape of the feature: after Dimas mentions her, his plain
    follow-ups run and Budi's asides do not."""
    gw, store, manager, _gate = _gateway()

    await gw.on_message("C1", "111.0", "just chatting", "U11111", False)
    assert manager.calls == []  # nobody has addressed her yet

    await gw.on_mention(channel="C1", thread_ts="111.0", text="hey <@UBOT>", author_id="U11111")
    manager.calls.clear()

    await gw.on_message("C1", "111.0", "budi's aside", "U22222", False)
    assert manager.calls == []  # not the partner -> no turn

    await gw.on_message("C1", "111.0", "follow-up message", "U11111", False)
    assert manager.calls == [("C1", "111.0", "follow-up message")]
    assert await store.get_partner("C1", "111.0") == "U11111"


async def test_mention_by_a_second_person_takes_over_the_conversation():
    gw, store, manager, _gate = _gateway()
    await store.set_partner("C1", "111.0", "U11111")

    await gw.on_mention(channel="C1", thread_ts="111.0", text="<@UBOT> over here", author_id="U22222")

    assert await store.get_partner("C1", "111.0") == "U22222"
    # And now the old partner's plain message is the one that gets ignored.
    manager.calls.clear()
    await gw.on_message("C1", "111.0", "wait, what about my thing", "U11111", False)
    assert manager.calls == []


async def test_dm_message_always_handled_and_sets_partner():
    gw, store, manager, _gate = _gateway()

    await gw.on_message("D1", "111.0", "hi jean", "U11111", True)

    assert await store.get_partner("D1", "111.0") == "U11111"
    assert manager.calls == [("D1", "111.0", "hi jean")]


async def test_mention_of_someone_else_clears_the_partner():
    gw, store, manager, _gate = _gateway()
    await store.set_partner("C1", "111.0", "U11111")

    await gw.on_message("C1", "111.0", "hey <@U22222>", "U11111", False)

    assert await store.get_partner("C1", "111.0") is None
    assert manager.calls == []


async def test_blocked_author_is_ignored():
    gw, store, manager, _gate = _gateway(soul=_soul(blocked_users=["U66666"]))
    await store.set_partner("C1", "111.0", "U66666")

    await gw.on_message("C1", "111.0", "anything", "U66666", False)

    assert manager.calls == []


async def test_ignored_message_does_not_write_to_the_store():
    """An ignored message must cost nothing -- no turn AND no write. If this
    regresses, every bystander's message silently starts hitting the database."""
    gw, store, manager, _gate = _gateway()
    await store.set_partner("C1", "111.0", "U11111")
    writes: list[str | None] = []
    original = store.set_partner

    async def spy(channel, thread_ts, user_id):
        writes.append(user_id)
        await original(channel, thread_ts, user_id)

    store.set_partner = spy

    await gw.on_message("C1", "111.0", "budi's aside", "U22222", False)

    assert manager.calls == []
    assert writes == []
```

- [ ] **Step 6: Run the tests to verify they fail**

Run: `uv run pytest tests/test_gateway_app.py -v`
Expected: FAIL — `TypeError: decide() got an unexpected keyword argument 'engaged'`.
`Gateway.on_message` still passes `engaged=`, but Step 3 just replaced that parameter
with `partner=`. (`set_partner` itself already exists — Task 1 added it.)

- [ ] **Step 7: Rewrite the `Gateway` methods**

In `src/jean/gateway/app.py`, replace `on_mention` and `on_message`:

```python
    async def on_mention(
        self, *, channel: str, thread_ts: str, text: str, author_id: str | None
    ) -> None:
        """Owns bot-@mentions: the author becomes this thread's conversation partner,
        then dispatch. Slack also delivers the same @mention as a plain `message`
        event (see `on_message`, which skips bot-mentions to avoid running the turn
        twice)."""
        if author_id is not None and author_id in self._soul_provider().blocked_users:
            return
        await self._store.set_partner(channel, thread_ts, author_id)
        await dispatch(self._manager, channel=channel, thread_ts=thread_ts, text=text)

    async def on_message(
        self, channel: str, thread_ts: str, text: str, author_id: str, is_dm: bool
    ) -> None:
        if self._bot_id in mentions_in(text):
            # `on_mention` (app_mention event) already engages + dispatches
            # this turn; handling it here too would run it twice.
            return
        partner = await self._store.get_partner(channel, thread_ts)
        decision = decide(
            bot_id=self._bot_id,
            channel=channel,
            thread_ts=thread_ts,
            text=text,
            is_dm=is_dm,
            soul=self._soul_provider(),
            partner=partner,
            author_id=author_id,
        )
        # Write only on a real change: a bystander's message must cost nothing --
        # no turn, and no database write either.
        if decision.partner != partner:
            await self._store.set_partner(channel, thread_ts, decision.partner)
        if decision.handle:
            await dispatch(self._manager, channel=channel, thread_ts=thread_ts, text=text)
```

- [ ] **Step 8: Run the full gate**

Run: `./scripts/verify.sh`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/jean/gateway/engagement.py src/jean/gateway/app.py tests/test_engagement.py tests/test_gateway_app.py
git commit -m "feat(gateway): reply only to the thread's conversation partner"
```

---

### Task 3: Delete the dead `engaged` column and API

Nothing reads `engaged` any more. Remove it so the schema and the port tell the truth.

**Files:**
- Modify: `src/jean/ports.py`, `src/jean/db/memory.py`, `src/jean/db/postgres.py`,
  `tests/store_behavior.py`, `src/jean/config.py:69` (stale comment)

**Interfaces:**
- Consumes: everything from Tasks 1–2.
- Produces: a `SessionStore` with no `engaged` / `set_engaged` / `is_engaged`.

- [ ] **Step 1: Update the shared assertions first (they are the test)**

In `tests/store_behavior.py::assert_session_roundtrip_and_engagement`, delete the
three `engaged` assertions (`is_engaged`, `row.engaged is False`, the `set_engaged`
block, and `row.engaged is True`) so the function only covers session round-tripping.
Rename it to `assert_session_roundtrip` and update the two callers
(`tests/test_memory_store.py`, `tests/test_postgres_store.py`) and their imports.

The resulting function body:

```python
async def assert_session_roundtrip(store) -> None:
    channel, thread_ts = "C1", "111.222"
    assert await store.get_session(channel, thread_ts) is None

    await store.upsert_session(channel, thread_ts, sdk_session_id="sdk-abc")
    row = await store.get_session(channel, thread_ts)
    assert row is not None
    assert row.channel == channel
    assert row.thread_ts == thread_ts
    assert row.sdk_session_id == "sdk-abc"
    assert row.last_active_at > 0

    await store.upsert_session(channel, thread_ts, permission_mode="plan", touch=False)
    row = await store.get_session(channel, thread_ts)
    assert row.permission_mode == "plan"
    # sdk_session_id must survive an update that doesn't touch it.
    assert row.sdk_session_id == "sdk-abc"
```

- [ ] **Step 2: Run the tests to verify they still pass**

Run: `uv run pytest tests/test_memory_store.py -v`
Expected: PASS (the assertions were removed, the implementation still has the field).

- [ ] **Step 3: Remove `engaged` from the port**

In `src/jean/ports.py`: delete `engaged: bool` from `SessionRow`, delete the
`engaged: bool | None = None` parameter from `upsert_session`, and delete the
`set_engaged` and `is_engaged` lines from the `SessionStore` protocol.

`SessionRow` becomes:

```python
@dataclass
class SessionRow:
    channel: str
    thread_ts: str
    sdk_session_id: str | None
    permission_mode: str | None
    last_active_at: float
    turn_seq: int = 0
    engaged_with: str | None = None
```

- [ ] **Step 4: Remove `engaged` from `MemoryStore`**

In `src/jean/db/memory.py`: drop `engaged=row.engaged,` from `get_session`, drop the
`engaged` parameter and its line from `upsert_session`, and delete the `set_engaged`
and `is_engaged` methods.

- [ ] **Step 5: Remove `engaged` from `PostgresStore`**

In `src/jean/db/postgres.py`:

Delete the `engaged boolean NOT NULL DEFAULT false,` line from the `sessions`
`CREATE TABLE`, and add a DROP next to the other ALTERs:

```sql
ALTER TABLE sessions DROP COLUMN IF EXISTS engaged;
```

Drop `engaged=r["engaged"],` from `get_session`, delete the `set_engaged` and
`is_engaged` methods, and rewrite `upsert_session` without the column (note the
parameters renumber — `touch` moves from `$6` to `$5`):

```python
    async def upsert_session(
        self,
        channel: str,
        thread_ts: str,
        *,
        sdk_session_id: str | None = None,
        permission_mode: str | None = None,
        touch: bool = True,
    ) -> None:
        # COALESCE keeps existing values when a field is not being changed.
        await self._pool.execute(
            """INSERT INTO sessions(channel,thread_ts,sdk_session_id,permission_mode,last_active_at)
               VALUES($1,$2,$3,$4,
                      CASE WHEN $5 THEN extract(epoch from now()) ELSE 0 END)
               ON CONFLICT(channel,thread_ts) DO UPDATE SET
                 sdk_session_id=COALESCE($3, sessions.sdk_session_id),
                 permission_mode=COALESCE($4, sessions.permission_mode),
                 last_active_at=CASE WHEN $5 THEN extract(epoch from now()) ELSE sessions.last_active_at END""",
            channel,
            thread_ts,
            sdk_session_id,
            permission_mode,
            touch,
        )
```

- [ ] **Step 6: Fix the stale comment**

`src/jean/config.py:69` says a pruned session "also drops its transcript (FK cascade)
-- and its engaged/permission_mode". Change `engaged/permission_mode` to
`engaged_with/permission_mode`.

- [ ] **Step 7: Confirm nothing references `engaged` any more**

Run: `grep -rn "\bengaged\b\|set_engaged\|is_engaged" src tests`
Expected: no matches (only `engaged_with` should appear, which this pattern excludes).

- [ ] **Step 8: Run the full gate**

Run: `./scripts/verify.sh`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/jean/ports.py src/jean/db/memory.py src/jean/db/postgres.py src/jean/config.py tests/store_behavior.py tests/test_memory_store.py tests/test_postgres_store.py
git commit -m "refactor(store): drop the superseded thread-wide engaged flag"
```

---

### Task 4: Tell anya the truth in her prompt

The prompt currently describes the *old* rule. It must describe what the gateway
actually does, or she will reason about messages she can no longer see.

**Files:**
- Modify: `src/jean/persona/identity.py:32-35`

- [ ] **Step 1: Rewrite the Engagement paragraph**

Replace:

```
Engagement: you only participate in a thread once you have been engaged
(mentioned, DMed, or otherwise addressed) -- once engaged, keep replying to
follow-ups in that same thread until the human moves on or explicitly
disengages you.
```

with:

```
Engagement: you are only shown messages addressed to you -- a mention, a DM, or
a plain follow-up from the person who most recently mentioned you in that
thread. Everything else said in the thread never reaches you, so do not assume
you have seen the whole conversation: other people may have been talking while
you were not listening. If a message refers to something you have no record of,
ask rather than guess.
```

- [ ] **Step 2: Run the full gate**

Run: `./scripts/verify.sh`
Expected: PASS. (Check whether any test asserts on this prompt text:
`grep -rn "Engagement:" tests` — if one does, update it to match.)

- [ ] **Step 3: Commit**

```bash
git add src/jean/persona/identity.py
git commit -m "feat(persona): describe mention-gated engagement in the prompt"
```

---

## Deploy note

Task 3 drops a column at boot (`_SCHEMA` runs on every worker start). During a
`kubectl rollout restart`, an *old* pod still serving traffic will error on its next
`upsert_session` once a new pod has dropped `sessions.engaged`. jean is a small
deployment on a mutable `0.4.0` tag, so the exposure is the seconds between the new
pod booting and the old one terminating. If that matters on the day, deploy with
`strategy: Recreate` (or scale to zero, then up) instead of a rolling restart.

Existing engagement state is deliberately not migrated. Live threads forget their
partner; someone re-@-mentions anya once. That is the intended cost.

# Mention-gated engagement

**Status:** implemented on branch `mention-gated-engagement`
**Date:** 2026-07-14

## Problem

Engagement is sticky per thread. Once anya is @-mentioned once,
`sessions.engaged` flips to true and *every* subsequent human message in that
thread runs a full agent turn — including side-chat between two colleagues that
was never addressed to her.

Two costs, and the second is the one that hurts:

1. **Noise.** She replies to asides that weren't for her.
2. **Latency.** Turns are serialized per thread by the `pg_advisory_xact_lock`.
   Unwanted turns don't just cost tokens, they occupy the queue — a message
   actually addressed to anya waits behind every aside posted before it. Two
   colleagues chatting for 30 seconds can add minutes to your answer.

## Behaviour

Engagement becomes **one conversation partner per thread**, not a thread-wide
flag. anya replies to the person who most recently addressed her, and to nobody
else.

In a channel thread, in order:

1. Author is blocked → ignore. *(unchanged)*
2. Text @-mentions anya → handle. The author becomes this thread's partner.
   The most recent mention wins, so a second person can take over the
   conversation simply by mentioning her.
3. Text @-mentions someone else, and not anya → ignore. **And if the author is the
   current partner, clear the partner** — she steps back, because the person she was
   helping has handed the thread to a human. A *bystander* mentioning someone else
   leaves the partner untouched.

   *(Amended during implementation. The original rule — inherited from the old code —
   cleared the partner regardless of who wrote the message, which let a bystander
   silently disengage anya from someone else's live conversation. That contradicts the
   invariant this whole feature rests on: nothing a bystander does may change the
   thread's engagement state. Bystanders are now inert in both directions — they cannot
   make her speak, and they cannot make her stop.)*
4. No mentions, and the author **is** the partner → handle. Partner unchanged.
   This is the friction-free follow-up: you don't re-@ her every line.
5. Otherwise → **ignore.** This is the new rule and the whole point. A plain
   message from anyone who is not the current partner costs zero: no turn, no
   tokens, and no time in the thread's lock queue.

DMs are unchanged: always handled, no mention required, author is the partner.

### Worked example

```
Dimas:  @anya the checkout pods are crashlooping     → TURN. partner = Dimas
Dimas:  ok show me the memory limits                 → TURN (partner follow-up)
Budi:   oh yeah that started after friday's deploy   → IGNORED
Budi:   i think Rian bumped the replica count        → IGNORED
Dimas:  can you raise the limit to 1Gi?              → TURN, immediately
```

Three turns where there are five today, and the last message does not queue
behind two turns nobody wanted.

## Design

### State

`sessions.engaged boolean` → `sessions.engaged_with text` (nullable): the Slack
user id of the thread's current partner, or `NULL` for nobody.

Schema is created idempotently at boot, but the deployed database already holds
the old column, so boot also runs
`ALTER TABLE sessions ADD COLUMN IF NOT EXISTS engaged_with text`.

*(Amended during implementation. The original design also dropped `engaged` here,
but `_SCHEMA` runs at every worker boot, so the DROP would take effect the moment
the first new pod connects — while a pod still running the previous image has an
`upsert_session` that names `engaged` in its INSERT, which then fails with
`UndefinedColumnError`. Worse, a rollback to that image would be permanently
broken, because `CREATE TABLE IF NOT EXISTS` will not re-add a column to a table
that already exists. The DROP is deferred instead: the dead `engaged` column stays
in Postgres — it's `NOT NULL DEFAULT false`, so the new INSERT's omission of it
succeeds on the default and costs nothing — until a later release, once no pod
running the old image can exist.)*

**Existing engagement state is deliberately not migrated.** The worst case is
that a live thread forgets its partner and someone re-@-mentions anya once. That
does not justify a data migration.

### Ports and adapters

`SessionStore` swaps `set_engaged(bool)` / `is_engaged() -> bool` for
`set_partner(user_id | None)` / `get_partner() -> str | None`. Both `db/memory.py`
and `db/postgres.py` implement it; `SessionRow.engaged: bool` becomes
`SessionRow.engaged_with: str | None`.

`gateway/engagement.py::decide()` takes `partner: str | None` in place of
`engaged: bool`, and `Decision.engage: bool | None` becomes
`Decision.partner: str | None` — always the *resulting* partner, never a
"leave it alone" sentinel. Since `decide()` already receives the current
partner, the unchanged cases just return it back, which keeps the three
outcomes (set / clear / unchanged) expressible in two states.

`gateway/app.py` then writes to the store **only when
`decision.partner != partner`**. This matters: it keeps an ignored message
genuinely free — no turn *and* no database write — which is the entire point of
the change.

`gateway/app.py::on_mention` sets the partner to the author instead of setting a
boolean. If Slack gives no author id, the turn is still handled but the partner is
left **unchanged** — an event we cannot attribute must not mutate who anya is
talking to.

*(Amended during implementation. The original rule stored `None`, which meant an
unattributable or bot-authored event could* wipe *a live partner — the same
bystander-interference hole through a different door. `register()`'s `app_mention`
handler now also drops messages carrying a `bot_id`, so another bot's "@anya look at
this" no longer takes the partner slot. It deliberately does NOT filter on `subtype`
the way the `message` handler does: a `file_share` — someone uploading a screenshot
captioned "@anya what's wrong here" — is a real, addressed mention, and `app_mention`
is the only handler that can serve it.)*

Both entry points (`on_mention` and `on_message`) route through `decide()` via a shared
`Gateway._engage`, so the blocked-user check and partner assignment have exactly one
source of truth. An earlier cut re-implemented them inside `on_mention`, which left
`decide()`'s bot-mention branch unreachable from the app — a trap for the next person to
add channel-scoping to `decide()`, since the primary `@anya` path would have silently
bypassed it.

### Prompt

`persona/identity.py:32` currently tells anya *"once engaged, keep replying to..."*.
That becomes false: the gateway no longer delivers those messages to her at all.
Rewrite it to say she only ever sees messages addressed to her.

## Testing

`tests/test_engagement.py` is already a table of pure `decide()` cases with no
I/O. New cases:

- partner's plain follow-up → handled
- non-partner's plain message → ignored (the regression this whole change exists
  to prevent)
- mention by a second person → handled, partner switches
- mention of a third party → ignored, partner cleared
- DM → handled regardless of partner

Store changes are covered through the in-memory fake, plus the existing asyncpg
integration test (skipped unless `JEAN_TEST_DATABASE_URL` is set).

## Out of scope

- **A follow-up timeout.** A partner stays the partner until someone else
  mentions her or a third party is mentioned. The failure this would guard
  against — you @-mention her Monday, post an unrelated message Thursday, she
  answers it — is rare, self-correcting, and easy to add later.
- **Buffering skipped messages as context.** Ignored messages are gone; anya
  will not know what Budi said. Accepted knowingly. If it bites, the upgrade is
  to buffer unaddressed messages and prepend them (with author attribution) to
  the next real turn.
- **Author attribution in the turn text.** `dispatch()` passes only `text`, so
  anya cannot tell who is speaking. A real gap in a multi-person thread, but a
  separate change.

# Slack read tools — design

**Date:** 2026-08-03
**Status:** approved, ready for planning

## Problem

`jean_slack` is write-only. Its tools are `reply`, `edit`, `upload`, `react`,
`unreact`, `request_approval` — every one of them emits. Nothing reads.

So a request like *"look at #sre-support for today's messages, and inside the
threads too"* is not a permissions failure the operator can fix by inviting the
bot somewhere; it is unimplementable with the current tool surface. The agent
correctly refused rather than inventing content, which is the behaviour we want
— but the refusal is a real capability gap, not a false negative.

## Goal

Let the agent read message history from a public Slack channel it has been
invited to, and read the replies under a specific message, so it can act on what
a channel actually said.

## Non-goals

- Private channels, DMs, group DMs. Public channels only (`channels:history`,
  `channels:read`). A private channel yields Slack's own error, surfaced
  verbatim — no silent empty result.
- `search.messages`. Bot-token search support is inconsistent across Slack app
  generations and the channel-scoped read covers the actual use case.
- Resolving user ids to display names. Ids stay verbatim as `<@U…>`.
- Any change to engagement, approval, or authorization logic.

## Tool surface

Two tools on the existing in-process `jean_slack` server, built per session by
`build_slack_mcp` exactly like the write tools.

### `read_channel`

```
channel   (required) "#sre-support" | "sre-support" | "C0123ABC"
oldest    (optional) epoch seconds, or ISO "2026-08-03" / "2026-08-03T09:00"
latest    (optional) same forms
timezone  (optional) IANA name, default "UTC" — how bare dates are read
limit     (optional) default 50, hard cap 200
```

Returns the messages in the window, oldest first.

### `read_thread`

```
channel   (required) same forms as above
thread_ts (required) the parent message ts, as printed by read_channel
limit     (optional) default 50, hard cap 200
```

Returns the parent plus its replies, oldest first.

**Why two tools rather than one `include_threads` flag.** The agent reads the
channel, sees which messages carry replies, and opens only the ones that matter.
A recursive single call would pull every thread in the window into context
whether or not it bears on the question — for a busy support channel that is the
difference between a usable turn and a blown context window.

## Rendered output

The tools return text, not JSON. One line per message:

```
[2026-08-03 09:14 Asia/Jakarta] <@U0123ABC> pods OOMKilling in prod (4 replies, ts=1754210040.123456)
```

Multi-line message text is indented under its header so the line structure
survives. `ts=` is printed because it is the handle for `read_thread`, `edit`,
and `react` — without it the agent can see a message but not act on it.

**User ids stay verbatim.** No `users:read`, no N+1 lookups per read, and when
the agent quotes an id back into a reply Slack renders it as a real mention.
This also matches the trust boundary's existing habit: ids move through the
system as the literal strings Slack gave us.

## Ports and adapters

`ChatSurface` (`ports.py`) gains three methods — this is the Slack boundary, and
reading Slack is the same boundary as writing it:

```python
async def history(self, channel: str, *, oldest: float | None,
                  latest: float | None, limit: int) -> list[Message]: ...
async def replies(self, channel: str, thread_ts: str, *,
                  limit: int) -> list[Message]: ...
async def resolve_channel(self, name_or_id: str) -> str: ...
```

New `Message` dataclass in `ports.py`: `ts, user, text, thread_ts, reply_count`.
Domain code never sees a Slack payload dict.

`SlackSurface` (`slack/client.py`) implements them over `conversations_history`,
`conversations_replies`, and `conversations_list`.

**Channel resolution.** A value matching `^[CG][A-Z0-9]+$` is passed through
untouched. Anything else is stripped of a leading `#` and looked up by name via
paged `conversations_list(types="public_channel")`, memoised per worker in a
plain dict — channel ids do not change, and a cold miss costs one paged scan per
worker lifetime rather than one per read. An unresolvable name is an error
naming the channel, not an empty result.

## The trust boundary

**No approval gate on reads.** Reading is non-mutating, and the authorization is
Slack's, not ours: `conversations.history` returns `not_in_channel` unless the
bot was explicitly invited. The decision therefore lives entirely outside the
LLM, which is the non-negotiable — the agent cannot talk its way into a channel
nobody invited it to.

**Accepted residual risk, stated plainly:** any public channel the bot is
invited to, the agent can be prompted into summarizing into a different thread.
The invite list is the control surface. This is a deliberate tradeoff — a
read-side approval gate would make every history call block on a human, which
defeats the feature — and it should be documented in the README next to the
scope list so operators size the invite list knowingly.

## Errors and limits

- `limit` above 200 is clamped to 200, and the output says so. Truncation is
  never silent: when the window held more messages than were returned, the last
  line states that older messages exist in the window.
- Slack API errors (`not_in_channel`, `channel_not_found`, missing scope) come
  back as `is_error: True` with Slack's error code included. Domain code does
  not swallow them.
- An empty window returns an explicit "no messages in this window" rather than
  an empty string, so the agent can distinguish "nothing happened" from "the
  call did nothing".

## Config and setup

README's scope list gains `channels:history` and `channels:read`, with a note
that the app must be re-installed for new scopes to take effect and that the bot
reads only channels it was invited to.

No new `JEAN_*` settings. The limits are constants; the timezone is per-call.

## Testing

Unit tests only, no live network, following `tests/test_slack_mcp.py`:

- `build_slack_mcp` exposes the two new tool names.
- `read_channel` handlers called directly via `<tool>.handler(args)`.
- A fake web client asserts the real Slack kwargs reach it — `oldest`/`latest`
  as epoch strings, `limit` clamped, `channel` resolved to an id.
- Date parsing: bare ISO date in a non-UTC zone spans that whole local day.
- `#name`, bare `name`, and a raw `C…` id all resolve; the paged scan is
  exercised across two pages and the cache prevents a second scan.
- Slack error → `is_error: True` carrying the Slack code.
- Rendering: multi-line text, reply counts, empty window, truncation notice.

# Slack Read Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the agent `read_channel` and `read_thread` tools so it can read history from public Slack channels it has been invited to.

**Architecture:** Two new tools on the existing in-process `jean_slack` MCP server. Reading is a Slack-boundary concern, so `ChatSurface` grows `history` / `replies` / `resolve_channel` and `SlackSurface` implements them over `conversations_history` / `conversations_replies` / `conversations_list`. Two pure helper modules — time-window parsing and message rendering — keep the parsing and formatting logic out of the adapter and testable without any Slack object at all.

**Tech Stack:** Python 3.11+, `slack_sdk` `AsyncWebClient` (adapter only), `claude_agent_sdk` `@tool`, pytest + pytest-asyncio (`asyncio_mode = "auto"`).

**Spec:** `docs/superpowers/specs/2026-08-03-slack-read-tools-design.md`

## Global Constraints

- Work in the worktree `.claude/worktrees/slack-read-tools` on branch `slack-read-tools`. All commands below assume that directory.
- `from __future__ import annotations` at the top of every new module. Modern hints (`str | None`, `list[str]`).
- Async on every I/O path. Domain methods that touch a port are `async`.
- Layering: `gateway/`, `session/`, `approval/`, `persona/` must not import `slack_sdk`. `slack/client.py` is an adapter and MAY import it. `slack/mcp.py` must NOT — it catches the domain error `ChatReadError` from `ports.py` instead.
- No live network in tests. Inject fakes at the ports.
- Test output must be pristine — no stray warnings.
- Run `./scripts/verify.sh` before every commit (ruff check + ruff format-check + pytest).
- Do NOT add AI co-author trailers to commits.
- Never name the company or its internal systems in code, comments, tests, docs, or commit messages. Use `example-org` / `*.internal.example`.
- Public channels only. Never add `groups:*`, `im:*`, or `mpim:*` scopes or code paths in this plan.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/jean/slack/timewindow.py` (new) | Parse an `oldest`/`latest` bound into epoch seconds. Pure. No Slack. |
| `src/jean/slack/render.py` (new) | Render `list[Message]` into the compact text the agent reads. Pure. |
| `src/jean/ports.py` (modify) | `Message` dataclass, `ChatReadError`, three new `ChatSurface` methods. |
| `src/jean/slack/client.py` (modify) | `SlackSurface.resolve_channel` / `.history` / `.replies`. |
| `src/jean/slack/mcp.py` (modify) | `read_channel` + `read_thread` tools. |
| `src/jean/persona/identity.py` (modify) | Baseline prompt tells the agent the read tools exist. |
| `README.md` (modify) | `channels:history` + `channels:read` scopes, invite-list caveat. |
| `tests/test_slack_timewindow.py` (new) | Bound parsing, timezones, errors. |
| `tests/test_slack_render.py` (new) | Line format, multi-line, empty, truncation. |
| `tests/test_slack_read.py` (new) | `SlackSurface` against a fake web client. |
| `tests/test_slack_mcp.py` (modify) | The two new tools end-to-end through `.handler(...)`. |
| `tests/test_ports.py` (modify) | `StubChat` gains the new methods (see warning in Task 2). |

---

## Task 1: Time-window parsing

**Files:**
- Create: `src/jean/slack/timewindow.py`
- Test: `tests/test_slack_timewindow.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class TimeWindowError(ValueError)`
  - `def parse_bound(value: str | float | None, *, timezone: str, end_of_day: bool) -> float | None`

**Context you need:** The agent will pass `oldest`/`latest` as either epoch seconds (Slack's own `ts` format, e.g. `"1754210040.123456"`) or ISO (`"2026-08-03"`, `"2026-08-03T09:00"`). A bare date must mean the *whole local day*, which is why `end_of_day` exists — the same string `"2026-08-03"` becomes 00:00:00 as an `oldest` and 23:59:59.999999 as a `latest`.

Two verified Python behaviours this depends on:
- `date.fromisoformat("2026-08-03T09:00")` **raises** `ValueError` — it rejects time components. So trying `date` first and falling through to `datetime` correctly separates "bare date" from "date and time".
- `float("2026-08-03")` raises `ValueError`, so the epoch branch never swallows an ISO date.

The `>= 1_000_000_000` guard on the epoch branch stops a bare `"20260803"` from being read as an epoch (it would land in 1970). Slack did not exist before Sept 2001, so any real Slack timestamp clears that floor.

- [ ] **Step 1: Write the failing test**

Create `tests/test_slack_timewindow.py`:

```python
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from jean.slack.timewindow import TimeWindowError, parse_bound


def test_none_and_empty_are_no_bound():
    assert parse_bound(None, timezone="UTC", end_of_day=False) is None
    assert parse_bound("", timezone="UTC", end_of_day=False) is None
    assert parse_bound("   ", timezone="UTC", end_of_day=False) is None


def test_epoch_seconds_pass_through():
    assert parse_bound("1754210040.123456", timezone="UTC", end_of_day=False) == 1754210040.123456
    assert parse_bound(1754210040, timezone="UTC", end_of_day=False) == 1754210040.0


def test_bare_date_as_oldest_is_local_midnight():
    got = parse_bound("2026-08-03", timezone="Asia/Jakarta", end_of_day=False)
    expected = datetime(2026, 8, 3, 0, 0, tzinfo=ZoneInfo("Asia/Jakarta")).timestamp()
    assert got == expected


def test_bare_date_as_latest_is_end_of_local_day():
    """The same string means the whole day: this is what makes 'today' one call."""
    got = parse_bound("2026-08-03", timezone="Asia/Jakarta", end_of_day=True)
    start = parse_bound("2026-08-03", timezone="Asia/Jakarta", end_of_day=False)
    assert got - start == pytest.approx(86399.999999, abs=1e-3)


def test_bare_date_window_differs_by_zone():
    """A day in Jakarta is not the same 24 hours as a day in UTC -- the timezone
    argument has to actually reach the arithmetic, not just get validated."""
    jakarta = parse_bound("2026-08-03", timezone="Asia/Jakarta", end_of_day=False)
    utc = parse_bound("2026-08-03", timezone="UTC", end_of_day=False)
    assert utc - jakarta == 7 * 3600


def test_naive_datetime_is_read_in_the_given_zone():
    got = parse_bound("2026-08-03T09:00", timezone="Asia/Jakarta", end_of_day=False)
    expected = datetime(2026, 8, 3, 9, 0, tzinfo=ZoneInfo("Asia/Jakarta")).timestamp()
    assert got == expected


def test_explicit_offset_wins_over_the_timezone_argument():
    got = parse_bound("2026-08-03T09:00:00+07:00", timezone="UTC", end_of_day=False)
    expected = datetime(2026, 8, 3, 9, 0, tzinfo=ZoneInfo("Asia/Jakarta")).timestamp()
    assert got == expected


def test_digits_that_are_not_a_plausible_epoch_are_rejected():
    """'20260803' looks like a date to a human and like 1970 to float()."""
    with pytest.raises(TimeWindowError):
        parse_bound("20260803", timezone="UTC", end_of_day=False)


def test_unparseable_bound_names_the_accepted_forms():
    with pytest.raises(TimeWindowError) as exc:
        parse_bound("last tuesday", timezone="UTC", end_of_day=False)
    assert "YYYY-MM-DD" in str(exc.value)


def test_unknown_timezone_is_a_time_window_error():
    with pytest.raises(TimeWindowError) as exc:
        parse_bound("2026-08-03", timezone="Mars/Olympus", end_of_day=False)
    assert "Mars/Olympus" in str(exc.value)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd .claude/worktrees/slack-read-tools && uv run pytest tests/test_slack_timewindow.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jean.slack.timewindow'`

- [ ] **Step 3: Write minimal implementation**

Create `src/jean/slack/timewindow.py`:

```python
from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Slack did not exist before Sept 2001, so anything below this is not an epoch
# the agent meant. Without the floor, float() reads a bare "20260803" as 1970
# and the window silently returns nothing instead of erroring.
_EPOCH_FLOOR = 1_000_000_000

_ACCEPTED = "epoch seconds, 'YYYY-MM-DD', or 'YYYY-MM-DDTHH:MM'"


class TimeWindowError(ValueError):
    """A bound or timezone that cannot be turned into an epoch second."""


def zone(timezone: str) -> ZoneInfo:
    """Public because `slack/render.py` needs the SAME failure mode: an unknown
    timezone must raise TimeWindowError from either module, or a bad zone with
    no date bounds would escape as a raw ZoneInfoNotFoundError past the tool's
    error handling."""
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise TimeWindowError(f"unknown timezone {timezone!r}") from exc


def parse_bound(
    value: str | float | None, *, timezone: str, end_of_day: bool
) -> float | None:
    """One `oldest`/`latest` bound as epoch seconds, or None for "no bound".

    `end_of_day` decides what a BARE date means: an `oldest` of "2026-08-03"
    is that day's local midnight, a `latest` of the same string is the last
    instant of that day. That is what lets "today" be a single call with the
    same date in both slots.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        if seconds >= _EPOCH_FLOOR:
            return seconds
        raise TimeWindowError(f"cannot read {text!r} as a time; use {_ACCEPTED}")

    tz = zone(timezone)

    try:
        day = date.fromisoformat(text)
    except ValueError:
        pass  # has a time component (or is junk) -- the datetime branch decides
    else:
        moment = time(23, 59, 59, 999999) if end_of_day else time(0, 0)
        return datetime.combine(day, moment, tz).timestamp()

    try:
        moment_dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TimeWindowError(f"cannot read {text!r} as a time; use {_ACCEPTED}") from exc
    # A naive datetime is read in the caller's zone; an explicit offset wins.
    if moment_dt.tzinfo is None:
        moment_dt = moment_dt.replace(tzinfo=tz)
    return moment_dt.timestamp()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd .claude/worktrees/slack-read-tools && uv run pytest tests/test_slack_timewindow.py -q`
Expected: PASS — 10 passed.

- [ ] **Step 5: Verify and commit**

```bash
cd .claude/worktrees/slack-read-tools
./scripts/verify.sh
git add src/jean/slack/timewindow.py tests/test_slack_timewindow.py
git commit -m "feat(slack): parse read-window bounds into epoch seconds"
```

---

## Task 2: `Message`, `ChatReadError`, and rendering

**Files:**
- Modify: `src/jean/ports.py`
- Create: `src/jean/slack/render.py`
- Test: `tests/test_slack_render.py`
- Modify: `tests/test_ports.py` (`StubChat`)

**Interfaces:**
- Consumes: `zone` and `TimeWindowError` from `jean.slack.timewindow` (Task 1) — `render.py` reuses the bound parser's timezone resolution so a bad zone fails identically in both.
- Produces:
  - `@dataclass Message: ts: str; user: str; text: str; thread_ts: str | None = None; reply_count: int = 0`
  - `class ChatReadError(RuntimeError)` with `.code: str`
  - `ChatSurface.history`, `.replies`, `.resolve_channel` (protocol methods only — implemented in Task 3/4)
  - `def render_messages(messages: list[Message], *, timezone: str = "UTC", truncated: bool = False) -> str`

**⚠️ Known breakage this task must fix:** `tests/test_ports.py:129` asserts `isinstance(StubChat(), ChatSurface)`. `ChatSurface` is `@runtime_checkable`, so that isinstance check verifies method *presence*. Adding three methods to the protocol makes that assertion fail until `StubChat` gains them. Fix `StubChat` in the same task — the failure is expected, not a surprise.

**Context you need:** Rendering is deliberately text, not JSON — JSON of a Slack payload is mostly keys the agent will never use, and a support channel's worth of it blows the context window. The `ts=` on each line is load-bearing: it is the handle the agent passes to `read_thread`, `edit`, and `react`. Drop it and the agent can see a message but cannot act on it.

User ids stay verbatim `<@U…>`. No `users:read` lookup — that would be an extra scope plus one API call per distinct author, and the raw form renders as a real mention when the agent quotes it back into a reply.

- [ ] **Step 1: Write the failing test**

Create `tests/test_slack_render.py`:

```python
from __future__ import annotations

import pytest

from jean.ports import Message
from jean.slack.render import render_messages
from jean.slack.timewindow import TimeWindowError


# 1785746040 is 2026-08-03 15:34 in Asia/Jakarta -- verified, not guessed. If you
# change it, recompute the expected string in test_line_carries_time_author_and_ts.
def _msg(**kw):
    base = {"ts": "1785746040.123456", "user": "U0123ABC", "text": "hello"}
    return Message(**{**base, **kw})


def test_empty_is_explicit_not_blank():
    """An empty string reads as 'the tool did nothing'; the agent has to be able
    to tell that apart from 'the channel was quiet'."""
    out = render_messages([], timezone="UTC")
    assert "no messages" in out.lower()


def test_line_carries_time_author_and_ts():
    out = render_messages([_msg(text="pods OOMKilling")], timezone="Asia/Jakarta")
    assert "2026-08-03 15:34 Asia/Jakarta" in out
    assert "<@U0123ABC>" in out
    assert "ts=1785746040.123456" in out
    assert "pods OOMKilling" in out


def test_reply_count_is_shown_only_when_there_are_replies():
    with_replies = render_messages([_msg(reply_count=4)], timezone="UTC")
    without = render_messages([_msg(reply_count=0)], timezone="UTC")
    assert "4 replies" in with_replies
    assert "replies" not in without


def test_single_reply_is_not_pluralised():
    assert "1 reply," in render_messages([_msg(reply_count=1)], timezone="UTC")


def test_multiline_text_is_indented_under_its_header():
    """Line structure has to survive a message that contains newlines, or the
    agent cannot tell where one message ends and the next begins."""
    out = render_messages([_msg(text="line one\nline two")], timezone="UTC")
    lines = out.splitlines()
    assert lines[0].endswith("line one")
    assert lines[1] == "    line two"


def test_messages_render_oldest_first_in_given_order():
    out = render_messages(
        [_msg(ts="1785746040.000000", text="first"), _msg(ts="1785749640.000000", text="second")],
        timezone="UTC",
    )
    assert out.index("first") < out.index("second")


def test_truncation_is_stated_not_silent():
    out = render_messages([_msg()], timezone="UTC", truncated=True)
    assert "older messages" in out.lower()


def test_missing_author_renders_as_unknown_not_none():
    out = render_messages([_msg(user="")], timezone="UTC")
    assert "None" not in out
    assert "unknown" in out.lower()


def test_unknown_timezone_raises_the_same_error_the_bound_parser_does():
    """One failure mode for a bad zone, whether or not date bounds were given --
    otherwise a raw ZoneInfoNotFoundError escapes the tool's error handling."""
    with pytest.raises(TimeWindowError):
        render_messages([_msg()], timezone="Mars/Olympus")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd .claude/worktrees/slack-read-tools && uv run pytest tests/test_slack_render.py -q`
Expected: FAIL — `ImportError: cannot import name 'Message' from 'jean.ports'`

- [ ] **Step 3a: Add `Message` and `ChatReadError` to `src/jean/ports.py`**

Add next to the other dataclasses (after `PruneResult`):

```python
@dataclass
class Message:
    """One Slack message, flattened to what the agent actually reads.

    `user` is the raw Slack id -- never a resolved display name. Ids travel
    through jean as the literal strings Slack gave us, and the raw form renders
    as a real mention when the agent quotes it back.
    """

    ts: str
    user: str
    text: str
    thread_ts: str | None = None
    reply_count: int = 0


class ChatReadError(RuntimeError):
    """A read the chat surface refused. `code` is the provider's own error
    string (Slack's `not_in_channel`, `channel_not_found`, ...), carried so the
    tool can hand the agent the real reason instead of an empty result.

    Defined here, in the port module, so `slack/mcp.py` can catch it without
    importing `slack_sdk` -- the layering rule keeps concrete infra in the
    adapters."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code
```

And extend the `ChatSurface` protocol with the three read methods:

```python
    async def resolve_channel(self, name_or_id: str) -> str: ...
    async def history(
        self,
        channel: str,
        *,
        oldest: float | None = None,
        latest: float | None = None,
        limit: int = 50,
    ) -> tuple[list[Message], bool]: ...
    async def replies(
        self, channel: str, thread_ts: str, *, limit: int = 50
    ) -> tuple[list[Message], bool]: ...
```

Both readers return `(messages, has_more)` — the caller needs to know it hit the
limit so truncation can be *stated* rather than silently swallowed.

- [ ] **Step 3b: Add the same three methods to `StubChat` in `tests/test_ports.py`**

Inside `class StubChat`, after `set_status`:

```python
    async def resolve_channel(self, name_or_id):
        return "C1"

    async def history(self, channel, *, oldest=None, latest=None, limit=50):
        return ([], False)

    async def replies(self, channel, thread_ts, *, limit=50):
        return ([], False)
```

- [ ] **Step 3c: Create `src/jean/slack/render.py`**

```python
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from jean.ports import Message
from jean.slack.timewindow import zone as _resolve_zone

_INDENT = "    "


def _when(ts: str, tz: ZoneInfo, timezone: str) -> str:
    try:
        moment = datetime.fromtimestamp(float(ts), tz)
    except (ValueError, OSError):  # a ts Slack should never send, but might
        return ts
    return f"{moment:%Y-%m-%d %H:%M} {timezone}"


def render_messages(
    messages: list[Message], *, timezone: str = "UTC", truncated: bool = False
) -> str:
    """The compact text form the agent reads. Oldest first, in the order given.

    Text, not JSON: a Slack payload is mostly keys the agent never uses, and a
    busy channel's worth of them is the difference between a usable turn and a
    blown context window. `ts=` stays on every line because it is the handle for
    read_thread/edit/react -- without it the agent can see a message but not act
    on it."""
    if not messages:
        return "(no messages in this window)"

    # Same TimeWindowError as the bound parser raises -- one failure mode for a
    # bad timezone, whether or not any date bounds were given.
    tz = _resolve_zone(timezone)
    lines: list[str] = []
    for message in messages:
        meta = [f"ts={message.ts}"]
        if message.reply_count:
            noun = "reply" if message.reply_count == 1 else "replies"
            meta.insert(0, f"{message.reply_count} {noun}")
        author = f"<@{message.user}>" if message.user else "(unknown author)"
        body = message.text or "(no text)"
        head, _, rest = body.partition("\n")
        lines.append(
            f"[{_when(message.ts, tz, timezone)}] {author} ({', '.join(meta)}) {head}"
        )
        for line in rest.splitlines():
            lines.append(f"{_INDENT}{line}")

    if truncated:
        lines.append("")
        lines.append("(truncated at the limit -- older messages exist in this window)")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd .claude/worktrees/slack-read-tools && uv run pytest tests/test_slack_render.py tests/test_ports.py -q`
Expected: PASS — the render suite plus the whole ports suite, including `test_stub_chat_satisfies_protocol`.

- [ ] **Step 5: Verify and commit**

```bash
cd .claude/worktrees/slack-read-tools
./scripts/verify.sh
git add src/jean/ports.py src/jean/slack/render.py tests/test_slack_render.py tests/test_ports.py
git commit -m "feat(slack): Message port type and compact read rendering"
```

---

## Task 3: `SlackSurface.resolve_channel`

**Files:**
- Modify: `src/jean/slack/client.py`
- Test: `tests/test_slack_read.py` (create)

**Interfaces:**
- Consumes: `ChatReadError` from `jean.ports` (Task 2).
- Produces: `async def resolve_channel(self, name_or_id: str) -> str` on `SlackSurface`.

**Context you need:** The agent will say `#sre-support`, because that is what a human typed. Slack's API wants `C0123ABC`. `conversations.list` is the only way across, it pages, and it is slow on a large workspace — so the result is memoised per worker in a plain dict. Channel ids do not change, so a cold miss costs one paged scan per worker lifetime rather than one per read.

An id is passed through untouched (`^[CGD][A-Z0-9]+$`). Note `G`/`D` match too even though this feature is public-channels-only: passing them through means Slack returns its own honest `not_in_channel` / missing-scope error, which is more useful to the agent than jean inventing a "that looks private" message.

An unresolvable name raises `ChatReadError("channel_not_found")`. It must never return an empty result — "I read the channel and it was empty" and "there is no such channel" are different answers.

- [ ] **Step 1: Write the failing test**

Create `tests/test_slack_read.py`:

```python
from __future__ import annotations

import pytest

from jean.ports import ChatReadError
from jean.slack.client import SlackSurface


class FakeWeb:
    """Stands in for AsyncWebClient. Records the kwargs each call received so
    tests assert what actually reaches Slack, not that a mock was called."""

    def __init__(self, *, pages=None, history=None, replies=None):
        self._pages = pages or [{"channels": [], "response_metadata": {}}]
        self._history = history or {"messages": [], "has_more": False}
        self._replies = replies or {"messages": [], "has_more": False}
        self.list_calls: list[dict] = []
        self.history_calls: list[dict] = []
        self.replies_calls: list[dict] = []

    async def conversations_list(self, **kwargs):
        self.list_calls.append(kwargs)
        cursor = kwargs.get("cursor") or ""
        index = 0 if not cursor else int(cursor)
        return self._pages[index]

    async def conversations_history(self, **kwargs):
        self.history_calls.append(kwargs)
        return self._history

    async def conversations_replies(self, **kwargs):
        self.replies_calls.append(kwargs)
        return self._replies


def _pages(*groups):
    """Build conversations_list pages; every page but the last hands on a cursor."""
    out = []
    for i, group in enumerate(groups):
        last = i == len(groups) - 1
        out.append(
            {
                "channels": group,
                "response_metadata": {} if last else {"next_cursor": str(i + 1)},
            }
        )
    return out


async def test_channel_id_passes_through_without_an_api_call():
    web = FakeWeb()
    surface = SlackSurface(web)
    assert await surface.resolve_channel("C0123ABC") == "C0123ABC"
    assert web.list_calls == []


async def test_hash_prefixed_name_resolves_to_id():
    web = FakeWeb(pages=_pages([{"id": "C999", "name": "sre-support"}]))
    surface = SlackSurface(web)
    assert await surface.resolve_channel("#sre-support") == "C999"


async def test_bare_name_resolves_to_id():
    web = FakeWeb(pages=_pages([{"id": "C999", "name": "sre-support"}]))
    surface = SlackSurface(web)
    assert await surface.resolve_channel("sre-support") == "C999"


async def test_resolution_pages_until_it_finds_the_channel():
    web = FakeWeb(
        pages=_pages(
            [{"id": "C1", "name": "general"}],
            [{"id": "C999", "name": "sre-support"}],
        )
    )
    surface = SlackSurface(web)
    assert await surface.resolve_channel("sre-support") == "C999"
    assert len(web.list_calls) == 2
    assert web.list_calls[0]["types"] == "public_channel"


async def test_resolution_is_cached_so_a_second_read_does_not_rescan():
    web = FakeWeb(pages=_pages([{"id": "C999", "name": "sre-support"}]))
    surface = SlackSurface(web)
    await surface.resolve_channel("sre-support")
    await surface.resolve_channel("sre-support")
    assert len(web.list_calls) == 1


async def test_unknown_channel_raises_rather_than_returning_empty():
    """'no such channel' and 'the channel was empty' are different answers and
    the agent must never conflate them."""
    web = FakeWeb(pages=_pages([{"id": "C1", "name": "general"}]))
    surface = SlackSurface(web)
    with pytest.raises(ChatReadError) as exc:
        await surface.resolve_channel("sre-support")
    assert exc.value.code == "channel_not_found"
    assert "sre-support" in str(exc.value)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd .claude/worktrees/slack-read-tools && uv run pytest tests/test_slack_read.py -q`
Expected: FAIL — `AttributeError: 'SlackSurface' object has no attribute 'resolve_channel'`

- [ ] **Step 3: Implement in `src/jean/slack/client.py`**

Add these imports at the top (keeping the existing ones):

```python
import re

from slack_sdk.errors import SlackApiError

from jean.ports import ChatReadError, Message
```

Add the module-level constant after the imports:

```python
# A Slack conversation id, which is passed through untouched. G/D are matched
# too even though reads are public-channel-only: letting them through means the
# agent gets Slack's own honest error rather than a guess made here.
_CHANNEL_ID = re.compile(r"^[CGD][A-Z0-9]+$")
_LIST_PAGE = 1000
```

In `SlackSurface.__init__`, add the cache:

```python
    def __init__(self, web_client: Any) -> None:
        self._client = web_client
        # name -> id, for this worker's lifetime. Channel ids are stable, so a
        # cold miss costs one paged scan per worker rather than one per read.
        self._channel_ids: dict[str, str] = {}
```

Add the method:

```python
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
```

And the shared error translator, at module level below `_CHANNEL_ID`:

```python
def _read_error(exc: SlackApiError) -> ChatReadError:
    """Slack's error string, carried into a domain error so `slack/mcp.py` can
    report the real reason without importing slack_sdk."""
    response = getattr(exc, "response", None)
    code = "slack_error"
    if response is not None:
        try:
            code = response.get("error") or code
        except (AttributeError, TypeError):  # pragma: no cover -- defensive
            pass
    return ChatReadError(code, f"Slack refused the read: {code}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd .claude/worktrees/slack-read-tools && uv run pytest tests/test_slack_read.py -q`
Expected: PASS — 6 passed.

- [ ] **Step 5: Verify and commit**

```bash
cd .claude/worktrees/slack-read-tools
./scripts/verify.sh
git add src/jean/slack/client.py tests/test_slack_read.py
git commit -m "feat(slack): resolve channel names to ids with a per-worker cache"
```

---

## Task 4: `SlackSurface.history` and `.replies`

**Files:**
- Modify: `src/jean/slack/client.py`
- Modify: `tests/test_slack_read.py`

**Interfaces:**
- Consumes: `Message`, `ChatReadError` (Task 2); `_read_error`, `FakeWeb` (Task 3).
- Produces:
  - `async def history(self, channel, *, oldest=None, latest=None, limit=50) -> tuple[list[Message], bool]`
  - `async def replies(self, channel, thread_ts, *, limit=50) -> tuple[list[Message], bool]`

**Context you need:** `conversations.history` returns messages **newest-first**; the renderer wants oldest-first, so the adapter reverses. `conversations.replies` already returns oldest-first with the parent as the first element — do not reverse that one. Getting this backwards produces output that reads plausibly and is wrong, which is the worst failure mode here.

Slack wants `oldest`/`latest` as *strings* of epoch seconds. Passing a float works by accident today; format them explicitly.

Bot-authored messages carry `bot_id` and often no `user`. Fall back to `bot_id` so the line still names an author.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_slack_read.py`:

```python
def _raw(ts, text, user="U1", **extra):
    return {"ts": ts, "text": text, "user": user, **extra}


async def test_history_returns_oldest_first():
    """Slack hands back newest-first. The renderer reads top-to-bottom as a
    conversation, so the adapter is where the order gets fixed."""
    web = FakeWeb(
        history={
            "messages": [_raw("1754213640.0", "second"), _raw("1754210040.0", "first")],
            "has_more": False,
        }
    )
    messages, has_more = await SlackSurface(web).history("C1")
    assert [m.text for m in messages] == ["first", "second"]
    assert has_more is False


async def test_history_passes_window_and_limit_as_slack_expects():
    web = FakeWeb()
    await SlackSurface(web).history("C1", oldest=1754210040.0, latest=1754296440.0, limit=25)
    call = web.history_calls[0]
    assert call["channel"] == "C1"
    assert call["oldest"] == "1754210040.000000"
    assert call["latest"] == "1754296440.000000"
    assert call["limit"] == 25


async def test_history_omits_bounds_that_were_not_given():
    web = FakeWeb()
    await SlackSurface(web).history("C1")
    assert "oldest" not in web.history_calls[0]
    assert "latest" not in web.history_calls[0]


async def test_history_clamps_limit_to_the_cap():
    web = FakeWeb()
    await SlackSurface(web).history("C1", limit=9999)
    assert web.history_calls[0]["limit"] == 200


async def test_history_carries_reply_count_and_thread_ts():
    web = FakeWeb(
        history={
            "messages": [_raw("1754210040.0", "parent", thread_ts="1754210040.0", reply_count=4)],
            "has_more": False,
        }
    )
    messages, _ = await SlackSurface(web).history("C1")
    assert messages[0].reply_count == 4
    assert messages[0].thread_ts == "1754210040.0"


async def test_history_reports_has_more():
    web = FakeWeb(history={"messages": [_raw("1754210040.0", "x")], "has_more": True})
    _messages, has_more = await SlackSurface(web).history("C1")
    assert has_more is True


async def test_bot_message_falls_back_to_bot_id_for_the_author():
    web = FakeWeb(
        history={"messages": [{"ts": "1754210040.0", "text": "alert", "bot_id": "B7"}],
                 "has_more": False}
    )
    messages, _ = await SlackSurface(web).history("C1")
    assert messages[0].user == "B7"


async def test_replies_keep_slack_order_parent_first():
    """conversations.replies is ALREADY oldest-first with the parent leading --
    reversing it here would put the parent last and misread the thread."""
    web = FakeWeb(
        replies={
            "messages": [_raw("1754210040.0", "parent"), _raw("1754210100.0", "answer")],
            "has_more": False,
        }
    )
    messages, _ = await SlackSurface(web).replies("C1", "1754210040.0")
    assert [m.text for m in messages] == ["parent", "answer"]


async def test_replies_passes_ts_and_limit():
    web = FakeWeb()
    await SlackSurface(web).replies("C1", "1754210040.0", limit=10)
    call = web.replies_calls[0]
    assert call["channel"] == "C1"
    assert call["ts"] == "1754210040.0"
    assert call["limit"] == 10


async def test_slack_error_becomes_a_chat_read_error_carrying_the_code():
    from slack_sdk.errors import SlackApiError

    class Refusing(FakeWeb):
        async def conversations_history(self, **kwargs):
            raise SlackApiError("nope", {"ok": False, "error": "not_in_channel"})

    with pytest.raises(ChatReadError) as exc:
        await SlackSurface(Refusing()).history("C1")
    assert exc.value.code == "not_in_channel"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd .claude/worktrees/slack-read-tools && uv run pytest tests/test_slack_read.py -q`
Expected: FAIL — `AttributeError: 'SlackSurface' object has no attribute 'history'`

- [ ] **Step 3: Implement in `src/jean/slack/client.py`**

Add the cap constant next to `_LIST_PAGE`:

```python
# Slack allows more, but a bigger page buys context-window pressure, not value.
READ_LIMIT_MAX = 200
READ_LIMIT_DEFAULT = 50
```

Add the payload converter at module level:

```python
def _to_message(raw: dict[str, Any]) -> Message:
    # A bot post carries `bot_id` and usually no `user`; falling back keeps the
    # line attributed instead of rendering an anonymous message.
    return Message(
        ts=str(raw.get("ts") or ""),
        user=str(raw.get("user") or raw.get("bot_id") or ""),
        text=str(raw.get("text") or ""),
        thread_ts=raw.get("thread_ts"),
        reply_count=int(raw.get("reply_count") or 0),
    )
```

Add both methods to `SlackSurface`:

```python
    async def history(
        self,
        channel: str,
        *,
        oldest: float | None = None,
        latest: float | None = None,
        limit: int = READ_LIMIT_DEFAULT,
    ) -> tuple[list[Message], bool]:
        """A window of channel history, OLDEST FIRST, plus whether more exists.

        Slack returns newest-first; the flip happens here so every consumer
        reads a conversation top-to-bottom."""
        kwargs: dict[str, Any] = {
            "channel": channel,
            "limit": max(1, min(limit, READ_LIMIT_MAX)),
        }
        # Slack wants these as strings of epoch seconds.
        if oldest is not None:
            kwargs["oldest"] = f"{oldest:.6f}"
        if latest is not None:
            kwargs["latest"] = f"{latest:.6f}"
        try:
            resp = await self._client.conversations_history(**kwargs)
        except SlackApiError as exc:
            raise _read_error(exc) from exc
        raw = list(resp.get("messages") or [])
        raw.reverse()
        return [_to_message(m) for m in raw], bool(resp.get("has_more"))

    async def replies(
        self, channel: str, thread_ts: str, *, limit: int = READ_LIMIT_DEFAULT
    ) -> tuple[list[Message], bool]:
        """A thread's parent plus its replies. Slack already returns these
        oldest-first with the parent leading -- do NOT reverse."""
        try:
            resp = await self._client.conversations_replies(
                channel=channel,
                ts=thread_ts,
                limit=max(1, min(limit, READ_LIMIT_MAX)),
            )
        except SlackApiError as exc:
            raise _read_error(exc) from exc
        return [_to_message(m) for m in (resp.get("messages") or [])], bool(
            resp.get("has_more")
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd .claude/worktrees/slack-read-tools && uv run pytest tests/test_slack_read.py -q`
Expected: PASS — 16 passed.

- [ ] **Step 5: Verify and commit**

```bash
cd .claude/worktrees/slack-read-tools
./scripts/verify.sh
git add src/jean/slack/client.py tests/test_slack_read.py
git commit -m "feat(slack): read channel history and thread replies"
```

---

## Task 5: The `read_channel` and `read_thread` tools

**Files:**
- Modify: `src/jean/slack/mcp.py`
- Modify: `tests/test_slack_mcp.py`

**Interfaces:**
- Consumes: `parse_bound`/`TimeWindowError` (Task 1), `render_messages` (Task 2), `ChatReadError` + `ChatSurface.history`/`.replies`/`.resolve_channel` (Tasks 2–4).
- Produces: tools named `read_channel` and `read_thread` in the tuple `build_slack_mcp` returns; tool ids `mcp__jean_slack__read_channel` / `mcp__jean_slack__read_thread`.

**Context you need:** These tools take an explicit `channel`, unlike every existing tool in this file — the write tools are bound to the session's thread on purpose, and reading somewhere else is the entire point of this feature. That asymmetry is deliberate; do not "fix" it by binding the read tools to the session channel.

**No approval gate.** Reads are non-mutating and the authorization is Slack's: `conversations.history` returns `not_in_channel` unless the bot was invited. Calling `gate.request` here would make every read block on a human and defeat the feature.

The read tools land in `allowed_tools` automatically — `build_slack_mcp` returns every tool name and `agent_options.py` passes the whole list. That is correct: jean's own non-mutating tools are allow-listed, and `_SPOKE_TOOLS` in `session/session.py` lists only `reply`/`edit`/`upload`, so reading will not be mistaken for the agent having answered. **No change is needed in either file — verify, do not edit.**

- [ ] **Step 1: Write the failing test**

Add to `tests/test_slack_mcp.py`. First extend `FakeChat` (after `set_status`):

```python
    async def resolve_channel(self, name_or_id):
        self.resolved.append(name_or_id)
        if name_or_id.strip("#") == "nope":
            raise ChatReadError("channel_not_found", "no public channel named 'nope'")
        return "C-RESOLVED"

    async def history(self, channel, *, oldest=None, latest=None, limit=50):
        self.history_calls.append(
            {"channel": channel, "oldest": oldest, "latest": latest, "limit": limit}
        )
        return (self.history_result, self.history_has_more)

    async def replies(self, channel, thread_ts, *, limit=50):
        self.replies_calls.append(
            {"channel": channel, "thread_ts": thread_ts, "limit": limit}
        )
        return (self.replies_result, False)
```

and add to `FakeChat.__init__`:

```python
        self.resolved: list[str] = []
        self.history_calls: list[dict] = []
        self.replies_calls: list[dict] = []
        self.history_result: list[Message] = []
        self.history_has_more = False
        self.replies_result: list[Message] = []
```

with the imports at the top of the file:

```python
from jean.ports import ChatReadError, Message
```

Then the tests:

```python
def _tools_for(chat):
    _server, _names, tools = build_slack_mcp(chat, _make_gate(True), channel="C1", thread_ts="1.0")
    return {t.name: t for t in tools}


async def test_read_tools_are_exposed():
    tools = _tools_for(FakeChat())
    assert "read_channel" in tools
    assert "read_thread" in tools


async def test_read_channel_resolves_the_name_and_renders_the_messages():
    chat = FakeChat()
    chat.history_result = [Message(ts="1754210040.0", user="U9", text="pods down")]
    result = await _tools_for(chat)["read_channel"].handler({"channel": "#sre-support"})

    assert chat.resolved == ["#sre-support"]
    assert chat.history_calls[0]["channel"] == "C-RESOLVED"
    text = result["content"][0]["text"]
    assert "<@U9>" in text
    assert "pods down" in text
    assert "ts=1754210040.0" in text


async def test_read_channel_turns_a_bare_date_into_a_whole_local_day():
    """This is the 'today's messages' path: one date in both slots."""
    chat = FakeChat()
    await _tools_for(chat)["read_channel"].handler(
        {"channel": "C1", "oldest": "2026-08-03", "latest": "2026-08-03",
         "timezone": "Asia/Jakarta"}
    )
    call = chat.history_calls[0]
    assert call["latest"] - call["oldest"] == pytest.approx(86399.999999, abs=1e-3)


async def test_read_channel_defaults_to_utc_when_no_timezone_given():
    chat = FakeChat()
    await _tools_for(chat)["read_channel"].handler({"channel": "C1", "oldest": "2026-08-03"})
    expected = datetime(2026, 8, 3, tzinfo=ZoneInfo("UTC")).timestamp()
    assert chat.history_calls[0]["oldest"] == expected


async def test_read_channel_states_truncation_instead_of_hiding_it():
    chat = FakeChat()
    chat.history_result = [Message(ts="1754210040.0", user="U9", text="x")]
    chat.history_has_more = True
    result = await _tools_for(chat)["read_channel"].handler({"channel": "C1"})
    assert "older messages" in result["content"][0]["text"].lower()


async def test_read_channel_reports_an_unreadable_channel_as_an_error():
    """not_in_channel must reach the agent as an error, never as 'no messages'."""
    chat = FakeChat()
    result = await _tools_for(chat)["read_channel"].handler({"channel": "#nope"})
    assert result.get("is_error") is True
    assert "channel_not_found" in result["content"][0]["text"]


async def test_read_channel_rejects_an_unparseable_date_without_calling_slack():
    chat = FakeChat()
    result = await _tools_for(chat)["read_channel"].handler(
        {"channel": "C1", "oldest": "last tuesday"}
    )
    assert result.get("is_error") is True
    assert chat.history_calls == []


async def test_read_thread_passes_the_parent_ts_through():
    chat = FakeChat()
    chat.replies_result = [
        Message(ts="1754210040.0", user="U9", text="parent"),
        Message(ts="1754210100.0", user="U8", text="answer"),
    ]
    result = await _tools_for(chat)["read_thread"].handler(
        {"channel": "#sre-support", "thread_ts": "1754210040.0"}
    )
    call = chat.replies_calls[0]
    assert call["channel"] == "C-RESOLVED"
    assert call["thread_ts"] == "1754210040.0"
    text = result["content"][0]["text"]
    assert text.index("parent") < text.index("answer")


async def test_read_tools_never_ask_for_approval():
    """A read that blocked on a human would defeat the feature; Slack's own
    membership check is the authorization."""
    chat = FakeChat()
    gate_calls = []

    class RecordingGate:
        async def request(self, channel, thread_ts, summary):
            gate_calls.append(summary)
            raise AssertionError("reads must not go through the approval gate")

    _s, _n, tools = build_slack_mcp(chat, RecordingGate(), channel="C1", thread_ts="1.0")
    by_name = {t.name: t for t in tools}
    await by_name["read_channel"].handler({"channel": "C1"})
    await by_name["read_thread"].handler({"channel": "C1", "thread_ts": "1.0"})
    assert gate_calls == []
```

Add these imports at the top of `tests/test_slack_mcp.py`:

```python
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
```

Also update `test_tool_names_are_namespaced_for_allowed_tools` — its `names ==` assertion is exhaustive and will fail until the two new names are added:

```python
    assert names == {
        "reply", "edit", "upload", "react", "unreact", "request_approval",
        "read_channel", "read_thread",
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd .claude/worktrees/slack-read-tools && uv run pytest tests/test_slack_mcp.py -q`
Expected: FAIL — `KeyError: 'read_channel'` and the tool-name set assertion.

- [ ] **Step 3: Implement in `src/jean/slack/mcp.py`**

Add imports:

```python
from jean.ports import ChatReadError, ChatSurface
from jean.slack.render import render_messages
from jean.slack.timewindow import TimeWindowError, parse_bound
```

Add the schemas next to `_UPLOAD_SCHEMA`:

```python
_CHANNEL_PROP = {
    "type": "string",
    "description": "channel name (#sre-support or sre-support) or id (C0123ABC)",
}

_READ_CHANNEL_SCHEMA = {
    "type": "object",
    "properties": {
        "channel": _CHANNEL_PROP,
        "oldest": {
            "type": "string",
            "description": (
                "start of the window: epoch seconds, 'YYYY-MM-DD' (that whole "
                "local day), or 'YYYY-MM-DDTHH:MM'"
            ),
        },
        "latest": {"type": "string", "description": "end of the window, same forms as oldest"},
        "timezone": {
            "type": "string",
            "description": "IANA timezone dates are read in, e.g. 'Asia/Jakarta'. Default UTC.",
        },
        "limit": {"type": "integer", "description": "max messages, default 50, capped at 200"},
    },
    "required": ["channel"],
}

_READ_THREAD_SCHEMA = {
    "type": "object",
    "properties": {
        "channel": _CHANNEL_PROP,
        "thread_ts": {
            "type": "string",
            "description": "the parent message's ts, as printed by read_channel",
        },
        "timezone": {
            "type": "string",
            "description": "IANA timezone timestamps are shown in, e.g. 'Asia/Jakarta'. Default UTC.",
        },
        "limit": {"type": "integer", "description": "max messages, default 50, capped at 200"},
    },
    "required": ["channel", "thread_ts"],
}
```

Add the handlers inside `build_slack_mcp`, after `_request_approval`:

```python
    def _error(text: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": text}], "is_error": True}

    async def _read_channel(args: dict[str, Any]) -> dict[str, Any]:
        timezone = str(args.get("timezone") or "UTC")
        try:
            # The same bare date means midnight as a start and end-of-day as an
            # end, so "today" is one call with one date in both slots.
            oldest = parse_bound(args.get("oldest"), timezone=timezone, end_of_day=False)
            latest = parse_bound(args.get("latest"), timezone=timezone, end_of_day=True)
        except TimeWindowError as exc:
            return _error(str(exc))
        try:
            target = await chat.resolve_channel(str(args["channel"]))
            messages, has_more = await chat.history(
                target,
                oldest=oldest,
                latest=latest,
                limit=int(args.get("limit") or 50),
            )
            rendered = render_messages(messages, timezone=timezone, truncated=has_more)
        except ChatReadError as exc:
            return _error(f"could not read that channel ({exc.code}): {exc}")
        except TimeWindowError as exc:  # a bad timezone with no date bounds
            return _error(str(exc))
        return {"content": [{"type": "text", "text": rendered}]}

    async def _read_thread(args: dict[str, Any]) -> dict[str, Any]:
        timezone = str(args.get("timezone") or "UTC")
        try:
            target = await chat.resolve_channel(str(args["channel"]))
            messages, has_more = await chat.replies(
                target, str(args["thread_ts"]), limit=int(args.get("limit") or 50)
            )
            rendered = render_messages(messages, timezone=timezone, truncated=has_more)
        except ChatReadError as exc:
            return _error(f"could not read that thread ({exc.code}): {exc}")
        except TimeWindowError as exc:  # an unknown timezone reaches rendering
            return _error(str(exc))
        return {"content": [{"type": "text", "text": rendered}]}
```

Add both to the `tools` list, after `request_approval`:

```python
        tool(
            "read_channel",
            "Read recent messages from a public Slack channel this app has been "
            "invited to. Returns each message with its ts, which you pass to "
            "read_thread to open a thread.",
            _READ_CHANNEL_SCHEMA,
        )(_read_channel),
        tool(
            "read_thread",
            "Read the replies under one message, given the channel and the "
            "parent message's ts.",
            _READ_THREAD_SCHEMA,
        )(_read_thread),
```

Finally, extend the docstring note on `build_slack_mcp` — add this paragraph at the end:

```
    The read tools are the one exception to the per-thread binding above: they
    take an explicit `channel`, because reading somewhere OTHER than this thread
    is the whole point of them. They carry no approval gate -- a read mutates
    nothing, and Slack itself is the authorization (conversations.history fails
    with not_in_channel unless this app was invited), so the decision never
    reaches the model.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd .claude/worktrees/slack-read-tools && uv run pytest tests/test_slack_mcp.py -q`
Expected: PASS — all existing tests plus 9 new ones.

- [ ] **Step 5: Verify `_SPOKE_TOOLS` and `allowed_tools` need no change**

Run: `cd .claude/worktrees/slack-read-tools && uv run pytest -q`
Expected: PASS — the whole suite. Confirm by reading `src/jean/session/session.py:64-66` that `_SPOKE_TOOLS` still lists only `reply`/`edit`/`upload`, so a turn that only *reads* is still correctly treated as not having answered.

- [ ] **Step 6: Commit**

```bash
cd .claude/worktrees/slack-read-tools
./scripts/verify.sh
git add src/jean/slack/mcp.py tests/test_slack_mcp.py
git commit -m "feat(slack): read_channel and read_thread tools"
```

---

## Task 6: Tell the agent and the operator

**Files:**
- Modify: `src/jean/persona/identity.py`
- Modify: `README.md`
- Modify: `tests/test_persona_identity.py`

**Interfaces:**
- Consumes: the tool ids from Task 5.
- Produces: nothing other code depends on.

**Context you need:** The baseline prompt's Engagement paragraph currently ends with *"If a message refers to something you have no record of, ask rather than guess."* That advice was correct when the agent had no way to catch up. It now has one, and leaving the paragraph unchanged would keep the agent asking for a paste of messages it could read itself — exactly the failure this whole plan exists to fix.

The scope list in the README needs both scopes plus the honest note about what invitation implies.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_persona_identity.py`:

```python
def test_baseline_tells_the_agent_it_can_read_channel_history():
    composed = compose_system_prompt("persona text")
    assert "mcp__jean_slack__read_channel" in composed
    assert "mcp__jean_slack__read_thread" in composed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd .claude/worktrees/slack-read-tools && uv run pytest tests/test_persona_identity.py -q`
Expected: FAIL — `assert 'mcp__jean_slack__read_channel' in ...`

- [ ] **Step 3a: Update the Engagement paragraph in `src/jean/persona/identity.py`**

Replace the final sentence of the Engagement paragraph (*"If a message refers to something you have no record of, ask rather than guess."*) with:

```
If a message refers to something you have no record of, you can go and look:
`mcp__jean_slack__read_channel` reads recent messages from a public channel this
app has been invited to, and `mcp__jean_slack__read_thread` opens the replies
under one of them (pass the `ts` that read_channel printed). Reading is not
mutating, so it needs no approval. If the read fails because the app was never
invited to that channel, say so and ask to be invited -- do not ask for a paste
of something you could read yourself, and do not guess at what a channel said.
```

- [ ] **Step 3b: Update the scope list in `README.md`**

In the section *"3. Slack app setup"*, add after the `chat:write` bullet:

```markdown
- `channels:history` -- to read messages in public channels (`read_channel`,
  `read_thread`)
- `channels:read` -- to turn `#channel-name` into the id the API wants
```

And after the paragraph ending *"and optionally DM it directly."*, add:

```markdown
Adding scopes requires re-installing the app for them to take effect.

The read tools reach **only public channels the bot has been invited to** --
Slack refuses everything else, so the invite list is the real control surface.
Size it knowingly: anything jean can read, jean can be asked to summarize into
another thread.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd .claude/worktrees/slack-read-tools && uv run pytest tests/test_persona_identity.py -q`
Expected: PASS.

- [ ] **Step 5: Full gate and commit**

```bash
cd .claude/worktrees/slack-read-tools
./scripts/verify.sh
git add src/jean/persona/identity.py README.md tests/test_persona_identity.py
git commit -m "docs(slack): document read tools in the baseline prompt and README"
```

---

## Done when

- `uv run pytest -q` is green with no warnings.
- `./scripts/verify.sh` passes.
- `build_slack_mcp` returns eight tools; `read_channel` and `read_thread` appear in `allowed_tools` via the existing wiring.
- The README scope list names `channels:history` and `channels:read` and states the invite-list caveat.
- No `groups:*` / `im:*` / `mpim:*` scope or code path was added.

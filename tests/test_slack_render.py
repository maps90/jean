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

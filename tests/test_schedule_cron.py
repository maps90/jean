from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from jean.schedule.cron import CronError, next_after, validate


def _epoch(y: int, m: int, d: int, hh: int, mm: int, tz: str) -> float:
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(tz)).timestamp()


def test_next_after_advances_to_the_next_weekly_occurrence() -> None:
    # Tuesday 09:00 in Asia/Jakarta, asked from Tuesday 09:00 exactly.
    after = _epoch(2026, 8, 4, 9, 0, "Asia/Jakarta")
    got = next_after("0 9 * * 2", "Asia/Jakarta", after)
    assert got == _epoch(2026, 8, 11, 9, 0, "Asia/Jakarta")


def test_next_after_is_strictly_after_the_given_instant() -> None:
    # A run that fires at its due time must not compute itself as next.
    after = _epoch(2026, 8, 4, 9, 0, "Asia/Jakarta")
    assert next_after("0 9 * * 2", "Asia/Jakarta", after) > after


def test_next_after_crosses_a_month_boundary() -> None:
    after = _epoch(2026, 8, 31, 23, 0, "UTC")
    got = next_after("0 9 * * *", "UTC", after)
    assert got == _epoch(2026, 9, 1, 9, 0, "UTC")


def test_next_after_keeps_local_wall_clock_across_a_dst_shift() -> None:
    # Europe/London moves off BST on 2026-10-25. A 09:00 daily schedule must
    # still be 09:00 local the next morning, which is a different UTC offset --
    # this is the case naive epoch arithmetic gets wrong.
    after = _epoch(2026, 10, 24, 12, 0, "Europe/London")
    got = next_after("0 9 * * *", "Europe/London", after)
    local = datetime.fromtimestamp(got, ZoneInfo("Europe/London"))
    assert (local.year, local.month, local.day) == (2026, 10, 25)
    assert (local.hour, local.minute) == (9, 0)


def test_validate_rejects_a_malformed_expression() -> None:
    with pytest.raises(CronError):
        validate("not a cron", "UTC")


def test_validate_rejects_an_unknown_timezone() -> None:
    with pytest.raises(CronError):
        validate("0 9 * * 2", "Mars/Olympus_Mons")


def test_validate_accepts_a_good_expression() -> None:
    validate("0 9 * * 2", "Asia/Jakarta")

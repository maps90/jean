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

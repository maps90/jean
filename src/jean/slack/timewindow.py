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


def parse_bound(value: str | float | None, *, timezone: str, end_of_day: bool) -> float | None:
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

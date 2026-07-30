from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter


class CronError(ValueError):
    """A cron expression or timezone that cannot be scheduled."""


def _zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CronError(f"unknown timezone {timezone!r}") from exc


def validate(cron: str, timezone: str) -> None:
    """Raise CronError unless this pair can produce a next occurrence.

    Called before an approval is raised, so nobody is ever asked to approve a
    schedule that cannot run.
    """
    zone = _zone(timezone)
    if not croniter.is_valid(cron):
        raise CronError(f"invalid cron expression {cron!r}")
    # is_valid accepts field shapes that still cannot yield an occurrence
    # (e.g. Feb 30). Force one to prove the pair is schedulable.
    try:
        croniter(cron, datetime.now(zone)).get_next(datetime)
    except (ValueError, KeyError) as exc:
        raise CronError(f"cron {cron!r} yields no occurrence") from exc


def next_after(cron: str, timezone: str, after: float) -> float:
    """Epoch of the first occurrence strictly after `after`.

    Computed in local time, not UTC: "09:00 Tuesday" means 09:00 on the wall
    clock, which is a different UTC offset either side of a DST shift. croniter
    is given a zone-aware datetime so it advances local wall-clock fields and we
    convert back at the end.
    """
    zone = _zone(timezone)
    if not croniter.is_valid(cron):
        raise CronError(f"invalid cron expression {cron!r}")
    base = datetime.fromtimestamp(after, zone)
    return float(croniter(cron, base).get_next(datetime).timestamp())

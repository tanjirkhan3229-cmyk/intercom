"""Business-hours time arithmetic (P1.7 — RFC-000 §2.2, RFC-002 §2 R1).

Pure, DB-free engine used by SLA timers (RFC-002 §5.6) and the widget's expected-reply-time. An
office-hours schedule is a timezone + weekly recurring open intervals + full-day holidays;
"business time" is wall-clock time that falls inside those open intervals. All arithmetic is done
in the schedule's local timezone (so "9-5" means 9-5 local across DST), then returned as UTC.

The three operations SLA needs:
- ``add_business_time(bh, start, seconds)`` → the instant ``seconds`` of business time after
  ``start`` (turns an SLA target into a due-at).
- ``elapsed_business_seconds(bh, start, end)`` → business seconds within ``[start, end)``.
- ``is_open(bh, at)`` → whether the schedule is open at that instant.

Intervals are half-open ``[open, close)`` within one local day (``open < close <= 24:00``);
overnight spans are modelled by splitting across two weekdays. :func:`build_business_hours`
validates raw JSON input (raising ``ValidationError`` → 422) so a stored schedule is always
well-formed and the hot paths never need to re-validate.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from relay.core.errors import ValidationError

_DAY: int = 86_400  # seconds in a day (an interval's close may equal this = next local midnight)
# Bounded scan so a pathological all-closed calendar (or an absurd target) can never loop forever.
_MAX_SCAN_DAYS: int = 750
_TIME_RE = re.compile(r"^([01]\d|2[0-4]):([0-5]\d)$")
_WEEKDAYS = frozenset(range(7))  # 0=Mon .. 6=Sun (matches datetime.date.weekday())


@dataclass(frozen=True)
class Interval:
    """A half-open open-for-business span within a local day, in seconds from local midnight."""

    open_s: int
    close_s: int


@dataclass(frozen=True)
class BusinessHours:
    """A parsed, validated schedule. Build via :func:`build_business_hours` (never by hand)."""

    tz: ZoneInfo
    # weekday (0=Mon..6=Sun) -> ascending, non-overlapping intervals. Absent weekday = closed.
    weekly: Mapping[int, tuple[Interval, ...]]
    holidays: frozenset[dt.date]

    @property
    def weekly_open_seconds(self) -> int:
        """Total open seconds in a week — zero means the schedule can never accrue business time."""
        return sum(iv.close_s - iv.open_s for ivs in self.weekly.values() for iv in ivs)


# --- Parsing / validation (the 422 boundary) ----------------------------------


def _parse_hhmm(value: object, *, field: str) -> int:
    if not isinstance(value, str) or not _TIME_RE.match(value):
        raise ValidationError(
            "time must be 'HH:MM' between 00:00 and 24:00",
            details={"field": field, "value": value},
        )
    hh_s, mm_s = value.split(":")
    hh, mm = int(hh_s), int(mm_s)
    if hh == 24 and mm != 0:
        raise ValidationError("24:MM is only valid as 24:00", details={"field": field})
    return hh * 3600 + mm * 60


def _parse_intervals(raw: object, *, weekday: int) -> tuple[Interval, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise ValidationError(
            "weekly[day] must be a list of intervals", details={"weekday": weekday}
        )
    out: list[Interval] = []
    for idx, item in enumerate(raw):
        field = f"weekly[{weekday}][{idx}]"
        if isinstance(item, Mapping):
            open_v, close_v = item.get("open"), item.get("close")
        elif isinstance(item, Sequence) and not isinstance(item, str | bytes) and len(item) == 2:
            open_v, close_v = item[0], item[1]
        else:
            raise ValidationError(
                "interval must be {open, close} or [open, close]", details={"field": field}
            )
        open_s = _parse_hhmm(open_v, field=f"{field}.open")
        close_s = _parse_hhmm(close_v, field=f"{field}.close")
        if open_s >= close_s:
            raise ValidationError(
                "interval 'open' must be before 'close'", details={"field": field}
            )
        out.append(Interval(open_s, close_s))
    out.sort(key=lambda iv: iv.open_s)
    for prev, nxt in pairwise(out):
        if nxt.open_s < prev.close_s:
            raise ValidationError("intervals overlap", details={"weekday": weekday})
    return tuple(out)


def build_business_hours(
    timezone: str,
    weekly: Mapping[str, object] | None,
    holidays: Iterable[object] | None,
) -> BusinessHours:
    """Validate raw schedule input into a :class:`BusinessHours`. Raises ``ValidationError`` (422)
    on an unknown timezone, malformed time, overlapping/ inverted interval, or bad holiday date."""
    try:
        tz = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError("unknown timezone", details={"timezone": timezone}) from exc

    parsed: dict[int, tuple[Interval, ...]] = {}
    for key, raw in (weekly or {}).items():
        try:
            weekday = int(key)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "weekly keys must be a weekday 0-6 (Mon=0)", details={"key": key}
            ) from exc
        if weekday not in _WEEKDAYS:
            raise ValidationError("weekly keys must be a weekday 0-6 (Mon=0)", details={"key": key})
        intervals = _parse_intervals(raw, weekday=weekday)
        if intervals:
            parsed[weekday] = intervals

    holiday_set: set[dt.date] = set()
    for item in holidays or []:
        if not isinstance(item, str):
            raise ValidationError("holiday must be an ISO date string", details={"value": item})
        try:
            holiday_set.add(dt.date.fromisoformat(item))
        except ValueError as exc:
            raise ValidationError("holiday must be 'YYYY-MM-DD'", details={"value": item}) from exc

    return BusinessHours(tz=tz, weekly=parsed, holidays=frozenset(holiday_set))


# --- Core arithmetic ----------------------------------------------------------


def _ensure_aware(instant: dt.datetime) -> dt.datetime:
    if instant.tzinfo is None:
        raise ValidationError("instant must be timezone-aware")
    return instant


def _local_seconds(instant: dt.datetime, tz: ZoneInfo) -> tuple[dt.date, int]:
    """The local calendar date and wall-clock seconds-from-midnight of ``instant`` in ``tz``."""
    local = instant.astimezone(tz)
    return local.date(), local.hour * 3600 + local.minute * 60 + local.second


def _at(tz: ZoneInfo, day: dt.date, sec: int) -> dt.datetime:
    """Compose a local (date, seconds-from-midnight) back into a UTC instant. ``sec`` may be
    ``_DAY`` (next local midnight). Wall-clock addition then normalise — "5pm local" stays 5pm
    local across a DST change within the span."""
    midnight = dt.datetime(day.year, day.month, day.day, tzinfo=tz)
    return (midnight + dt.timedelta(seconds=sec)).astimezone(dt.UTC)


def _intervals_on(bh: BusinessHours, day: dt.date) -> tuple[Interval, ...]:
    if day in bh.holidays:
        return ()
    return bh.weekly.get(day.weekday(), ())


def is_open(bh: BusinessHours, at: dt.datetime) -> bool:
    """Whether the schedule is open at ``at`` (an aware instant)."""
    day, sec = _local_seconds(_ensure_aware(at), bh.tz)
    return any(iv.open_s <= sec < iv.close_s for iv in _intervals_on(bh, day))


def add_business_time(bh: BusinessHours, start: dt.datetime, seconds: int) -> dt.datetime:
    """Return the UTC instant ``seconds`` of *business* time after ``start``.

    Walks forward day-by-day from ``start``'s local date, consuming each open interval's remaining
    time until the target is met; holidays and closed weekdays contribute nothing. Raises
    ``ValidationError`` if the schedule has no open time (a due-at could never be reached).
    """
    if seconds < 0:
        raise ValidationError("seconds must be non-negative")
    start = _ensure_aware(start)
    if seconds == 0:
        return start.astimezone(dt.UTC)
    if bh.weekly_open_seconds == 0:
        raise ValidationError("office-hours schedule has no open time")

    day, cursor = _local_seconds(start, bh.tz)
    remaining = seconds
    for _ in range(_MAX_SCAN_DAYS):
        for iv in _intervals_on(bh, day):
            seg_start = max(cursor, iv.open_s)
            if seg_start >= iv.close_s:
                continue
            available = iv.close_s - seg_start
            if remaining <= available:
                return _at(bh.tz, day, seg_start + remaining)
            remaining -= available
        day = day + dt.timedelta(days=1)
        cursor = 0
    raise ValidationError("business-hours horizon exceeded")  # pragma: no cover


def elapsed_business_seconds(bh: BusinessHours, start: dt.datetime, end: dt.datetime) -> int:
    """Business seconds within ``[start, end)`` (0 if ``end <= start``)."""
    start = _ensure_aware(start)
    end = _ensure_aware(end)
    if end <= start:
        return 0
    start_day, start_sec = _local_seconds(start, bh.tz)
    end_day, end_sec = _local_seconds(end, bh.tz)
    total = 0
    day = start_day
    for _ in range(_MAX_SCAN_DAYS):
        lo_bound = start_sec if day == start_day else 0
        hi_bound = end_sec if day == end_day else _DAY
        for iv in _intervals_on(bh, day):
            lo = max(lo_bound, iv.open_s)
            hi = min(hi_bound, iv.close_s)
            if hi > lo:
                total += hi - lo
        if day >= end_day:
            return total
        day = day + dt.timedelta(days=1)
    # Reached only for a window wider than the scan horizon; raise rather than silently
    # under-count (mirrors add_business_time).
    raise ValidationError("business-hours window exceeds the scan horizon")  # pragma: no cover

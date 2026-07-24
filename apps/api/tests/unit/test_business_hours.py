"""Unit + property tests for the business-hours engine (P1.7).

Covers the P1.7 acceptance seed — "SLA timer fires business-hours-correct across a weekend" — plus
round-trip / monotonicity / 24-7-equivalence properties that pin the arithmetic the SLA timers
stand on. Pure module: no DB, no containers, so these run in the unit tier.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from relay.core.errors import ValidationError
from relay.modules.messaging.business_hours import (
    add_business_time,
    build_business_hours,
    elapsed_business_seconds,
    is_open,
)

_UTC = dt.UTC


def _mon_fri_9_5(tz: str = "UTC") -> object:
    """Mon-Fri 09:00-17:00 (8h/day, 40h/week)."""
    return build_business_hours(
        tz, {str(d): [{"open": "09:00", "close": "17:00"}] for d in range(5)}, []
    )


def _always(tz: str = "UTC") -> object:
    """24/7 — business time equals wall-clock time."""
    return build_business_hours(tz, {str(d): [["00:00", "24:00"]] for d in range(7)}, [])


# --- concrete cases -----------------------------------------------------------


def test_weekend_fixture_skips_saturday_and_sunday() -> None:
    """Acceptance seed: Fri 16:00 + 2h business → Mon 10:00 (the weekend contributes nothing)."""
    bh = _mon_fri_9_5("UTC")
    start = dt.datetime(2021, 1, 1, 16, 0, tzinfo=_UTC)  # a Friday
    assert start.weekday() == 4
    got = add_business_time(bh, start, 2 * 3600)
    assert got == dt.datetime(2021, 1, 4, 10, 0, tzinfo=_UTC)  # the following Monday


def test_within_a_single_day() -> None:
    bh = _mon_fri_9_5("UTC")
    start = dt.datetime(2021, 1, 1, 9, 30, tzinfo=_UTC)  # Friday, open
    assert add_business_time(bh, start, 3600) == dt.datetime(2021, 1, 1, 10, 30, tzinfo=_UTC)


def test_start_before_open_counts_from_open() -> None:
    bh = _mon_fri_9_5("UTC")
    start = dt.datetime(2021, 1, 1, 6, 0, tzinfo=_UTC)  # Friday, before open
    # First business second is 09:00; +1h ⇒ 10:00.
    assert add_business_time(bh, start, 3600) == dt.datetime(2021, 1, 1, 10, 0, tzinfo=_UTC)


def test_start_after_close_rolls_to_next_open_day() -> None:
    bh = _mon_fri_9_5("UTC")
    start = dt.datetime(2021, 1, 1, 20, 0, tzinfo=_UTC)  # Friday, after close
    assert add_business_time(bh, start, 3600) == dt.datetime(2021, 1, 4, 10, 0, tzinfo=_UTC)


def test_holiday_is_skipped() -> None:
    bh = build_business_hours(
        "UTC",
        {str(d): [{"open": "09:00", "close": "17:00"}] for d in range(5)},
        ["2021-01-04"],  # the Monday is a holiday
    )
    start = dt.datetime(2021, 1, 1, 16, 0, tzinfo=_UTC)  # Friday 16:00
    # Fri gives 1h, Sat/Sun closed, Mon is a holiday ⇒ lands Tue 2021-01-05 10:00.
    assert add_business_time(bh, start, 2 * 3600) == dt.datetime(2021, 1, 5, 10, 0, tzinfo=_UTC)


def test_is_open() -> None:
    bh = _mon_fri_9_5("UTC")
    assert is_open(bh, dt.datetime(2021, 1, 1, 12, 0, tzinfo=_UTC))  # Fri noon
    assert not is_open(bh, dt.datetime(2021, 1, 1, 17, 0, tzinfo=_UTC))  # Fri at close (half-open)
    assert not is_open(bh, dt.datetime(2021, 1, 2, 12, 0, tzinfo=_UTC))  # Saturday


def test_elapsed_over_a_weekend() -> None:
    bh = _mon_fri_9_5("UTC")
    fri = dt.datetime(2021, 1, 1, 12, 0, tzinfo=_UTC)  # Friday noon
    mon = dt.datetime(2021, 1, 4, 12, 0, tzinfo=_UTC)  # Monday noon
    # Fri 12:00-17:00 = 5h, Sat/Sun = 0, Mon 09:00-12:00 = 3h ⇒ 8h.
    assert elapsed_business_seconds(bh, fri, mon) == 8 * 3600


def test_timezone_is_respected() -> None:
    ny = ZoneInfo("America/New_York")
    bh = _mon_fri_9_5("America/New_York")
    start = dt.datetime(2021, 6, 2, 12, 0, tzinfo=ny)  # Wednesday noon NY (summer, EDT)
    got = add_business_time(bh, start, 3600)
    assert got == dt.datetime(2021, 6, 2, 13, 0, tzinfo=ny).astimezone(_UTC)


# --- validation (the 422 boundary) --------------------------------------------


@pytest.mark.parametrize(
    "tz,weekly,holidays",
    [
        ("Mars/Phobos", {}, []),  # unknown timezone
        ("UTC", {"0": [{"open": "25:00", "close": "26:00"}]}, []),  # bad hour
        ("UTC", {"0": [{"open": "09:60", "close": "17:00"}]}, []),  # bad minute
        ("UTC", {"0": [{"open": "17:00", "close": "09:00"}]}, []),  # inverted
        ("UTC", {"0": [["09:00", "12:00"], ["11:00", "15:00"]]}, []),  # overlap
        ("UTC", {"7": [["09:00", "17:00"]]}, []),  # weekday out of range
        ("UTC", {"0": [["09:00", "17:00"]]}, ["2021-13-40"]),  # bad holiday date
        ("UTC", {"0": [["09:00", "17:00"]]}, [12345]),  # non-string holiday
    ],
)
def test_build_rejects_bad_input(tz: str, weekly: dict, holidays: list) -> None:
    with pytest.raises(ValidationError):
        build_business_hours(tz, weekly, holidays)


def test_add_on_empty_schedule_raises() -> None:
    bh = build_business_hours("UTC", {}, [])
    with pytest.raises(ValidationError):
        add_business_time(bh, dt.datetime(2021, 1, 1, 9, 0, tzinfo=_UTC), 3600)


def test_naive_datetime_rejected() -> None:
    bh = _always()
    with pytest.raises(ValidationError):
        add_business_time(bh, dt.datetime(2021, 1, 1, 9, 0), 60)


# --- properties ---------------------------------------------------------------

# Whole-second aware UTC instants across 2000-2035 (no sub-second so composition is exact).
_instants = st.integers(min_value=946_684_800, max_value=2_082_758_400).map(
    lambda ts: dt.datetime.fromtimestamp(ts, _UTC)
)
# Durations up to ~23 days of business time — well inside the scan horizon.
_durations = st.integers(min_value=0, max_value=2_000_000)


@settings(max_examples=300, deadline=None)
@given(start=_instants, seconds=_durations)
def test_property_247_equals_wallclock(start: dt.datetime, seconds: int) -> None:
    bh = _always("UTC")
    assert add_business_time(bh, start, seconds) == start + dt.timedelta(seconds=seconds)
    assert elapsed_business_seconds(bh, start, start + dt.timedelta(seconds=seconds)) == seconds


@settings(max_examples=300, deadline=None)
@given(start=_instants, seconds=_durations)
def test_property_add_then_elapsed_roundtrips(start: dt.datetime, seconds: int) -> None:
    """The instant ``seconds`` of business time after ``start`` has exactly ``seconds`` of business
    time between it and ``start`` — the invariant the SLA due-at ↔ breach check relies on."""
    bh = _mon_fri_9_5("UTC")
    due = add_business_time(bh, start, seconds)
    assert elapsed_business_seconds(bh, start, due) == seconds


@settings(max_examples=200, deadline=None)
@given(start=_instants, a=_durations, b=_durations)
def test_property_add_is_monotonic(start: dt.datetime, a: int, b: int) -> None:
    bh = _mon_fri_9_5("UTC")
    lo, hi = sorted((a, b))
    assert add_business_time(bh, start, lo) <= add_business_time(bh, start, hi)

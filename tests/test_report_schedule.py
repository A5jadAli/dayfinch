from datetime import UTC, datetime, time, timedelta, timezone

import pytest

from api.services.report_schedule import (
    next_report_delivery,
    scheduled_report_retry_at,
    scheduled_report_window,
)


def test_daily_delivery_is_strictly_after_the_observation():
    morning = datetime(2026, 9, 7, 8, tzinfo=UTC)

    assert next_report_delivery(morning, "daily", time(9)) == datetime(
        2026, 9, 7, 9, tzinfo=UTC
    )
    assert next_report_delivery(
        morning + timedelta(hours=1), "daily", time(9)
    ) == datetime(2026, 9, 8, 9, tzinfo=UTC)


def test_weekly_delivery_uses_python_weekdays_and_rolls_forward():
    monday = datetime(2026, 9, 7, 10, tzinfo=UTC)

    assert next_report_delivery(monday, "weekly", time(9), weekday=4) == datetime(
        2026, 9, 11, 9, tzinfo=UTC
    )
    assert next_report_delivery(monday, "weekly", time(9), weekday=0) == datetime(
        2026, 9, 14, 9, tzinfo=UTC
    )


def test_monthly_delivery_handles_short_months_leap_years_and_year_rollover():
    assert next_report_delivery(
        datetime(2027, 1, 31, 12, tzinfo=UTC),
        "monthly",
        time(9, 30),
        month_day=1,
    ) == datetime(2027, 2, 1, 9, 30, tzinfo=UTC)
    assert next_report_delivery(
        datetime(2028, 2, 15, tzinfo=UTC),
        "monthly",
        time(9),
        month_day=-1,
    ) == datetime(2028, 2, 29, 9, tzinfo=UTC)
    assert next_report_delivery(
        datetime(2027, 12, 31, 23, tzinfo=UTC),
        "monthly",
        time(9),
        month_day=15,
    ) == datetime(2028, 1, 15, 9, tzinfo=UTC)


def test_delivery_normalizes_offsets_and_rejects_invalid_anchors():
    local = datetime(2026, 9, 7, 13, tzinfo=timezone(timedelta(hours=5)))
    assert next_report_delivery(local, "daily", time(9)) == datetime(
        2026, 9, 7, 9, tzinfo=UTC
    )
    with pytest.raises(ValueError, match="frequency"):
        next_report_delivery(local, "hourly", time(9))
    with pytest.raises(ValueError, match="weekday"):
        next_report_delivery(local, "weekly", time(9), weekday=7)
    with pytest.raises(ValueError, match="month day"):
        next_report_delivery(local, "monthly", time(9), month_day=31)


def test_scheduled_windows_use_only_completed_utc_periods():
    observed = datetime(2026, 9, 9, 14, 35, tzinfo=UTC)

    assert scheduled_report_window(observed, "daily", "previous_period") == (
        datetime(2026, 9, 8, tzinfo=UTC),
        datetime(2026, 9, 9, tzinfo=UTC),
    )
    assert scheduled_report_window(observed, "weekly", "previous_period") == (
        datetime(2026, 8, 31, tzinfo=UTC),
        datetime(2026, 9, 7, tzinfo=UTC),
    )
    assert scheduled_report_window(observed, "daily", "last_7_days") == (
        datetime(2026, 9, 2, tzinfo=UTC),
        datetime(2026, 9, 9, tzinfo=UTC),
    )


def test_previous_month_window_handles_leap_year_and_year_boundary():
    assert scheduled_report_window(
        datetime(2028, 3, 15, tzinfo=UTC), "monthly", "previous_month"
    ) == (
        datetime(2028, 2, 1, tzinfo=UTC),
        datetime(2028, 3, 1, tzinfo=UTC),
    )
    assert scheduled_report_window(
        datetime(2027, 1, 2, tzinfo=UTC), "monthly", "previous_period"
    ) == (
        datetime(2026, 12, 1, tzinfo=UTC),
        datetime(2027, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="range"):
        scheduled_report_window(datetime(2027, 1, 2, tzinfo=UTC), "monthly", "all_time")


def test_scheduled_retry_is_exponential_capped_and_stably_jittered():
    failed_at = datetime(2026, 9, 9, 12, tzinfo=UTC)
    first = scheduled_report_retry_at(failed_at, 0, "report-a")
    second = scheduled_report_retry_at(failed_at, 1, "report-a")
    capped = scheduled_report_retry_at(failed_at, 1_000, "report-a")

    assert timedelta(minutes=5) <= first - failed_at < timedelta(minutes=6, seconds=1)
    assert second - first == timedelta(minutes=5)
    assert (
        timedelta(hours=24)
        <= capped - failed_at
        < timedelta(hours=24, minutes=1, seconds=1)
    )
    assert scheduled_report_retry_at(failed_at, 0, "report-a") == first
    with pytest.raises(ValueError, match="cannot be negative"):
        scheduled_report_retry_at(failed_at, -1, "report-a")

from __future__ import annotations

import calendar
import hashlib
from datetime import UTC, datetime, time, timedelta

REPORT_RANGE_PRESETS = {
    "previous_period",
    "previous_day",
    "previous_week",
    "previous_month",
    "last_7_days",
    "last_30_days",
}


def _monthly_date(year: int, month: int, month_day: int) -> int:
    last_day = calendar.monthrange(year, month)[1]
    return last_day if month_day == -1 else min(month_day, last_day)


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def next_report_delivery(
    after: datetime,
    frequency: str,
    delivery_time: time,
    *,
    weekday: int = 0,
    month_day: int = 1,
) -> datetime:
    if frequency not in {"daily", "weekly", "monthly"}:
        raise ValueError("Invalid report frequency")
    if not 0 <= weekday <= 6:
        raise ValueError("Report weekday must be between 0 and 6")
    if month_day not in {1, 15, -1}:
        raise ValueError("Report month day must be 1, 15, or -1")
    if delivery_time.tzinfo is not None:
        raise ValueError("Report delivery time must be UTC without an offset")
    observed = (
        after.replace(tzinfo=UTC) if after.tzinfo is None else after.astimezone(UTC)
    )
    clock = delivery_time.replace(tzinfo=None)

    if frequency == "daily":
        candidate = datetime.combine(observed.date(), clock, UTC)
        return candidate if candidate > observed else candidate + timedelta(days=1)

    if frequency == "weekly":
        days_ahead = (weekday - observed.weekday()) % 7
        candidate = datetime.combine(
            observed.date() + timedelta(days=days_ahead), clock, UTC
        )
        return candidate if candidate > observed else candidate + timedelta(days=7)

    day = _monthly_date(observed.year, observed.month, month_day)
    candidate = datetime(observed.year, observed.month, day, tzinfo=UTC).replace(
        hour=clock.hour, minute=clock.minute, second=clock.second
    )
    if candidate > observed:
        return candidate
    year, month = _next_month(observed.year, observed.month)
    day = _monthly_date(year, month, month_day)
    return datetime(year, month, day, tzinfo=UTC).replace(
        hour=clock.hour, minute=clock.minute, second=clock.second
    )


def scheduled_report_window(
    observed_at: datetime, frequency: str, range_preset: str
) -> tuple[datetime, datetime]:
    """Return a completed, half-open UTC reporting window."""
    if frequency not in {"daily", "weekly", "monthly"}:
        raise ValueError("Invalid report frequency")
    if range_preset not in REPORT_RANGE_PRESETS:
        raise ValueError("Invalid report range")
    observed = (
        observed_at.replace(tzinfo=UTC)
        if observed_at.tzinfo is None
        else observed_at.astimezone(UTC)
    )
    today = datetime.combine(observed.date(), time.min, UTC)
    preset = range_preset
    if preset == "previous_period":
        preset = {
            "daily": "previous_day",
            "weekly": "previous_week",
            "monthly": "previous_month",
        }[frequency]
    if preset == "previous_day":
        return today - timedelta(days=1), today
    if preset == "last_7_days":
        return today - timedelta(days=7), today
    if preset == "last_30_days":
        return today - timedelta(days=30), today
    if preset == "previous_week":
        this_week = today - timedelta(days=today.weekday())
        return this_week - timedelta(days=7), this_week

    this_month = datetime(today.year, today.month, 1, tzinfo=UTC)
    previous_month_end = this_month
    previous_day = this_month - timedelta(days=1)
    previous_month_start = datetime(
        previous_day.year, previous_day.month, 1, tzinfo=UTC
    )
    return previous_month_start, previous_month_end


def scheduled_report_retry_at(
    failed_at: datetime, consecutive_failures: int, report_id: str
) -> datetime:
    """Return a capped exponential retry time with stable per-report jitter."""
    if consecutive_failures < 0:
        raise ValueError("Consecutive failures cannot be negative")
    observed = (
        failed_at.replace(tzinfo=UTC)
        if failed_at.tzinfo is None
        else failed_at.astimezone(UTC)
    )
    exponent = min(consecutive_failures, 16)
    delay_seconds = min(5 * 60 * (2**exponent), 24 * 60 * 60)
    digest = hashlib.sha256(report_id.encode("utf-8")).digest()
    jitter_seconds = int.from_bytes(digest[:2], "big") % 61
    return observed + timedelta(seconds=delay_seconds + jitter_seconds)

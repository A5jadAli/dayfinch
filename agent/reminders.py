from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _clock(value: object) -> time | None:
    if not isinstance(value, str) or len(value) != 5:
        return None
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError:
        return None


@dataclass(frozen=True)
class ReminderSettings:
    enabled: bool = False
    start: str = "09:00"
    end: str = "17:00"
    days: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri")
    interval_minutes: int = 15
    mode: str = "notification"

    @classmethod
    def from_mapping(cls, value: object, *, strict: bool = False) -> ReminderSettings:
        if not isinstance(value, dict):
            if strict:
                raise ValueError("Reminder settings must be an object")
            return cls()
        enabled = value.get("enabled", False)
        start = value.get("start", "09:00")
        end = value.get("end", "17:00")
        days = value.get("days", list(cls().days))
        interval = value.get("interval_minutes", 15)
        mode = value.get("mode", "notification")
        valid = (
            isinstance(enabled, bool)
            and _clock(start) is not None
            and _clock(end) is not None
            and start != end
            and isinstance(days, (list, tuple))
            and bool(days)
            and len(days) <= len(DAYS)
            and all(isinstance(day, str) and day in DAYS for day in days)
            and len(set(days)) == len(days)
            and isinstance(interval, int)
            and not isinstance(interval, bool)
            and 5 <= interval <= 240
            and mode in {"notification", "alert"}
        )
        if not valid:
            if strict:
                raise ValueError(
                    "Use valid start/end times, one or more unique days, a 5–240 "
                    "minute interval, and notification or alert mode"
                )
            return cls()
        ordered_days = tuple(day for day in DAYS if day in days)
        return cls(
            enabled=enabled,
            start=str(start),
            end=str(end),
            days=ordered_days,
            interval_minutes=interval,
            mode=str(mode),
        )

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["days"] = list(self.days)
        return result


@dataclass(frozen=True)
class ReminderWindow:
    key: str
    starts_at: datetime
    ends_at: datetime


def active_reminder_window(
    settings: ReminderSettings, moment: datetime
) -> ReminderWindow | None:
    if not settings.enabled:
        return None
    timezone = moment.tzinfo or UTC
    local = moment.replace(tzinfo=timezone) if moment.tzinfo is None else moment
    starts = _clock(settings.start)
    ends = _clock(settings.end)
    if starts is None or ends is None or starts == ends:
        return None
    candidates: list[ReminderWindow] = []
    for working_date in (local.date() - timedelta(days=1), local.date()):
        if DAYS[working_date.weekday()] not in settings.days:
            continue
        starts_at = datetime.combine(working_date, starts, timezone)
        end_date = working_date + timedelta(days=ends <= starts)
        ends_at = datetime.combine(end_date, ends, timezone)
        if starts_at <= local < ends_at:
            candidates.append(
                ReminderWindow(
                    key=f"{working_date}:{settings.start}:{settings.end}",
                    starts_at=starts_at,
                    ends_at=ends_at,
                )
            )
    return min(candidates, key=lambda window: window.starts_at) if candidates else None


def reminder_is_due(
    settings: ReminderSettings,
    moment: datetime,
    last_sent_at: datetime | None,
) -> ReminderWindow | None:
    window = active_reminder_window(settings, moment)
    if window is None:
        return None
    if last_sent_at is None:
        return window
    observed = moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)
    previous = (
        last_sent_at.astimezone(UTC)
        if last_sent_at.tzinfo
        else last_sent_at.replace(tzinfo=UTC)
    )
    elapsed = observed - previous
    # A clock correction must not silence reminders until a future timestamp.
    if elapsed < timedelta(0) or elapsed >= timedelta(
        minutes=settings.interval_minutes
    ):
        return window
    return None

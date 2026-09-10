from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class ActiveWindow:
    key: str
    project_id: str
    ends_at: datetime


def _clock(value: object) -> time | None:
    if not isinstance(value, str) or len(value) != 5:
        return None
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError:
        return None


def _fixed_window(policy: dict[str, Any], moment: datetime) -> ActiveWindow | None:
    schedule = policy.get("schedule")
    if not isinstance(schedule, dict):
        return None
    timezone = moment.tzinfo or UTC
    local = moment.replace(tzinfo=timezone) if moment.tzinfo is None else moment
    candidates: list[tuple[datetime, datetime, str]] = []
    # Yesterday is required for an overnight Monday 22:00–Tuesday 06:00 rule.
    for working_date in (local.date() - timedelta(days=1), local.date()):
        intervals = schedule.get(_DAYS[working_date.weekday()], [])
        if not isinstance(intervals, list):
            continue
        for interval in intervals[:8]:
            if not isinstance(interval, dict):
                continue
            starts = _clock(interval.get("start"))
            ends = _clock(interval.get("end"))
            if not starts or not ends or starts == ends:
                continue
            starts_at = datetime.combine(working_date, starts, timezone)
            end_date = (
                working_date + timedelta(days=1) if ends <= starts else working_date
            )
            ends_at = datetime.combine(end_date, ends, timezone)
            if starts_at <= local < ends_at:
                candidates.append(
                    (starts_at, ends_at, f"{working_date}:{starts}:{ends}")
                )
    if not candidates:
        return None
    starts_at, ends_at, identity = min(candidates, key=lambda value: value[0])
    return ActiveWindow(
        key=f"{policy.get('id', '')}:fixed:{identity}",
        project_id=str(policy.get("project_id", "")),
        ends_at=ends_at,
    )


def _shift_window(policy: dict[str, Any], moment: datetime) -> ActiveWindow | None:
    observed = moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)
    windows = policy.get("shift_windows")
    if not isinstance(windows, list):
        return None
    candidates: list[tuple[datetime, datetime, str]] = []
    for window in windows[:100]:
        if not isinstance(window, dict):
            continue
        try:
            starts_at = datetime.fromisoformat(str(window["starts_at"]))
            ends_at = datetime.fromisoformat(str(window["ends_at"]))
            if not starts_at.tzinfo or not ends_at.tzinfo:
                continue
            starts_at, ends_at = starts_at.astimezone(UTC), ends_at.astimezone(UTC)
        except (KeyError, TypeError, ValueError):
            continue
        if starts_at <= observed < ends_at:
            candidates.append(
                (
                    starts_at,
                    ends_at,
                    str(window.get("project_id") or policy.get("project_id", "")),
                )
            )
    if not candidates:
        return None
    starts_at, ends_at, project_id = min(candidates, key=lambda value: value[0])
    return ActiveWindow(
        key=f"{policy.get('id', '')}:shift:{starts_at.isoformat()}:{ends_at.isoformat()}",
        project_id=project_id,
        ends_at=ends_at,
    )


def active_window(
    policy: dict[str, Any] | None, moment: datetime
) -> ActiveWindow | None:
    if not policy or policy.get("consent_status") != "accepted":
        return None
    if policy.get("rule_type") == "fixed_schedule":
        return _fixed_window(policy, moment)
    if policy.get("rule_type") == "shifts":
        return _shift_window(policy, moment)
    return None

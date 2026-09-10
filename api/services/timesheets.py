from __future__ import annotations

from datetime import date
from typing import Any

from ..database import Database


class TimesheetService:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def parse_period(period_start: str, period_end: str) -> tuple[date, date]:
        try:
            start = date.fromisoformat(period_start)
            end = date.fromisoformat(period_end)
        except ValueError as exc:
            raise ValueError("Enter valid start and end dates") from exc
        if end < start:
            raise ValueError("Period end cannot be before its start")
        if (end - start).days > 31:
            raise ValueError("A timesheet period cannot exceed 32 days")
        return start, end

    def submit(
        self, user: dict[str, Any], period_start: str, period_end: str
    ) -> dict[str, Any]:
        start, end = self.parse_period(period_start, period_end)
        timesheet = self.database.submit_timesheet(user["id"], start, end)
        self.database.add_audit_event(
            user["id"],
            "timesheet.submitted",
            "timesheet",
            timesheet["id"],
            f"{start.isoformat()}..{end.isoformat()}",
        )
        return timesheet

    def review(
        self,
        admin: dict[str, Any],
        timesheet_id: str,
        decision: str,
        note: str,
    ) -> dict[str, Any]:
        timesheet = self.database.review_timesheet(
            timesheet_id, admin["id"], decision, note
        )
        self.database.add_audit_event(
            admin["id"],
            f"timesheet.{decision}",
            "timesheet",
            timesheet_id,
            note,
        )
        return timesheet

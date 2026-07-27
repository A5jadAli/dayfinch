from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from .base import RepositoryMixin, utc_now


class TimesheetRepository(RepositoryMixin):
    def submit_timesheet(
        self, user_id: str, period_start: date, period_end: date
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            open_session = connection.execute(
                """SELECT 1 FROM work_sessions
                   WHERE user_id = %s AND status IN ('active', 'paused')
                   LIMIT 1""",
                (user_id,),
            ).fetchone()
            if open_session:
                raise ValueError("Stop the active work session before submitting")
            existing = connection.execute(
                """SELECT * FROM timesheets
                   WHERE user_id = %s AND period_start = %s AND period_end = %s
                   FOR UPDATE""",
                (user_id, period_start, period_end),
            ).fetchone()
            if existing and existing["status"] == "approved":
                raise ValueError("Approved timesheets are locked")
            if existing:
                connection.execute(
                    """UPDATE timesheets SET status = 'submitted', submitted_at = %s,
                              reviewed_at = NULL, reviewed_by_user_id = NULL,
                              review_note = '', updated_at = %s
                       WHERE id = %s""",
                    (now, now, existing["id"]),
                )
                timesheet_id = existing["id"]
            else:
                timesheet_id = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO timesheets(
                           id, user_id, period_start, period_end, status,
                           submitted_at, created_at, updated_at
                       ) VALUES (%s, %s, %s, %s, 'submitted', %s, %s, %s)""",
                    (
                        timesheet_id,
                        user_id,
                        period_start,
                        period_end,
                        now,
                        now,
                        now,
                    ),
                )
            row = connection.execute(
                "SELECT * FROM timesheets WHERE id = %s", (timesheet_id,)
            ).fetchone()
        return dict(row)

    def review_timesheet(
        self,
        timesheet_id: str,
        reviewer_user_id: str,
        decision: str,
        note: str,
    ) -> dict[str, Any]:
        if decision not in {"approved", "rejected"}:
            raise ValueError("Decision must be approved or rejected")
        now = utc_now()
        with self.connect() as connection:
            current = connection.execute(
                "SELECT * FROM timesheets WHERE id = %s FOR UPDATE", (timesheet_id,)
            ).fetchone()
            if not current:
                raise ValueError("Timesheet not found")
            if current["status"] != "submitted":
                raise ValueError("Only submitted timesheets can be reviewed")
            connection.execute(
                """UPDATE timesheets
                   SET status = %s, reviewed_at = %s, reviewed_by_user_id = %s,
                       review_note = %s, updated_at = %s
                   WHERE id = %s""",
                (
                    decision,
                    now,
                    reviewer_user_id,
                    note.strip()[:500],
                    now,
                    timesheet_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM timesheets WHERE id = %s", (timesheet_id,)
            ).fetchone()
        return dict(row)

    def get_timesheet(self, timesheet_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM timesheets WHERE id = %s", (timesheet_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_timesheets(self, user_id: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE ts.user_id = %s" if user_id else ""
        parameters = (user_id,) if user_id else ()
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT ts.*, u.email,
                           COALESCE(SUM(GREATEST(0, EXTRACT(EPOCH FROM (
                               LEAST(COALESCE(seg.ended_at, CURRENT_TIMESTAMP),
                                     ts.period_end::timestamp + INTERVAL '1 day')
                               - GREATEST(seg.started_at, ts.period_start::timestamp)
                           )))), 0)::BIGINT AS tracked_seconds
                    FROM timesheets ts
                    JOIN users u ON u.id = ts.user_id
                    LEFT JOIN work_sessions ws
                      ON ws.user_id = ts.user_id
                     AND ws.started_at < ts.period_end::timestamp + INTERVAL '1 day'
                    LEFT JOIN work_session_segments seg
                      ON seg.session_id = ws.id
                     AND seg.started_at < ts.period_end::timestamp + INTERVAL '1 day'
                     AND COALESCE(seg.ended_at, CURRENT_TIMESTAMP) >= ts.period_start::timestamp
                    {where}
                    GROUP BY ts.id, u.email
                    ORDER BY ts.period_start DESC, u.email""",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def is_period_locked(self, user_id: str, work_date: date) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT 1 FROM timesheets
                   WHERE user_id = %s AND status = 'approved'
                     AND %s BETWEEN period_start AND period_end""",
                (user_id, work_date),
            ).fetchone()
        return row is not None

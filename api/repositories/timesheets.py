from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from .base import RepositoryMixin, utc_now


class TimesheetRepository(RepositoryMixin):
    def generate_open_timesheets(self, today: date | None = None) -> int:
        today = today or date.today()
        settings = self.organization_settings()
        frequency = settings["pay_period"]
        if frequency == "monthly":
            period_start = today.replace(day=1)
            next_month = (period_start.replace(day=28) + timedelta(days=4)).replace(
                day=1
            )
            period_end = next_month - timedelta(days=1)
        elif frequency == "biweekly":
            anchor = date(1970, 1, 5)
            period_start = anchor + timedelta(days=((today - anchor).days // 14) * 14)
            period_end = period_start + timedelta(days=13)
        else:
            week_start = int(settings["week_starts_on"])
            python_weekday = 6 if week_start == 0 else week_start - 1
            period_start = today - timedelta(
                days=(today.weekday() - python_weekday) % 7
            )
            period_end = period_start + timedelta(days=6)
        created = 0
        with self.connect() as connection:
            users = connection.execute(
                "SELECT id FROM users WHERE enabled=TRUE AND require_timesheet_approval=TRUE"
            ).fetchall()
            for user in users:
                result = connection.execute(
                    """INSERT INTO timesheets(id,user_id,period_start,period_end,status,submitted_at,created_at,updated_at)
                       VALUES (%s,%s,%s,%s,'open',NULL,%s,%s)
                       ON CONFLICT(user_id,period_start,period_end) DO NOTHING""",
                    (
                        str(uuid.uuid4()),
                        user["id"],
                        period_start,
                        period_end,
                        utc_now(),
                        utc_now(),
                    ),
                )
                created += max(0, result.rowcount)
        return created

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
            pending = connection.execute(
                """SELECT
                 EXISTS(SELECT 1 FROM manual_time_entries WHERE user_id=%s AND status='pending' AND started_at::date BETWEEN %s AND %s) manual,
                 EXISTS(SELECT 1 FROM time_off_requests WHERE user_id=%s AND status='pending' AND starts_on <= %s AND ends_on >= %s) time_off""",
                (user_id, period_start, period_end, user_id, period_end, period_start),
            ).fetchone()
            if pending["manual"] or pending["time_off"]:
                raise ValueError(
                    "Resolve pending manual-time and time-off requests before submitting"
                )
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
                f"""SELECT ts.*, u.email, u.full_name, u.pay_rate,
                           COALESCE(SUM(GREATEST(0, EXTRACT(EPOCH FROM (
                               LEAST(COALESCE(seg.ended_at, CURRENT_TIMESTAMP),
                                     ts.period_end::timestamp + INTERVAL '1 day')
                               - GREATEST(seg.started_at, ts.period_start::timestamp)
                           )))), 0)::BIGINT AS tracked_seconds,
                           COALESCE((SELECT SUM(EXTRACT(EPOCH FROM (m.ended_at-m.started_at)))
                             FROM manual_time_entries m WHERE m.user_id=ts.user_id AND m.status='approved'
                             AND m.started_at::date BETWEEN ts.period_start AND ts.period_end),0)::BIGINT manual_seconds,
                           COALESCE((SELECT SUM(r.minutes*60) FROM time_off_requests r
                             WHERE r.user_id=ts.user_id AND r.status='approved'
                             AND r.starts_on <= ts.period_end AND r.ends_on >= ts.period_start),0)::BIGINT pto_seconds,
                           COALESCE((SELECT SUM(h.paid_minutes*60) FROM holidays h
                             WHERE h.holiday_date BETWEEN ts.period_start AND ts.period_end),0)::BIGINT holiday_seconds,
                           COALESCE((SELECT ROUND(AVG(a.activity_percent)) FROM activity_records a
                             WHERE a.user_id=ts.user_id AND a.captured_at::date BETWEEN ts.period_start AND ts.period_end),0)::INTEGER activity_percent
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
                    GROUP BY ts.id, u.id
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

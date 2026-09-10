from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from ..services.quickbooks_iif import QuickBooksIIFError, validate_mapping
from ..services.report_schedule import REPORT_RANGE_PRESETS, next_report_delivery
from .base import RepositoryMixin, token_hash, utc_now


class WorkforceRepository(RepositoryMixin):
    """Queries and commands for the workforce-management side of Dayfinch."""

    TEAM_LEAD_PERMISSIONS = {
        "approve_timesheets": "can_approve_timesheets",
        "approve_manual_time": "can_approve_manual_time",
        "approve_time_off": "can_approve_time_off",
        "manage_schedules": "can_manage_schedules",
        "manage_projects": "can_manage_projects",
        "manage_members": "can_manage_members",
        "manage_financials": "can_manage_financials",
    }
    TRACKING_OVERRIDE_FIELDS = (
        "screenshot_frequency",
        "screenshot_blur",
        "track_apps",
        "track_urls",
        "allowed_apps",
        "idle_timeout_minutes",
        "allow_screenshot_delete",
    )

    def dashboard_summary(
        self, user_id: str | None = None, project_member_id: str | None = None
    ) -> dict[str, Any]:
        if user_id and project_member_id:
            raise ValueError("Choose a personal or project-member dashboard scope")
        if user_id:
            session_clause = "AND ws.user_id = %s"
            activity_clause = "AND a.user_id = %s"
            project_clause = (
                "WHERE EXISTS (SELECT 1 FROM project_members pm "
                "WHERE pm.project_id=p.id AND pm.user_id=%s)"
            )
            scope_params: tuple[Any, ...] = (user_id,)
        elif project_member_id:
            session_clause = (
                "AND EXISTS (SELECT 1 FROM project_members pm "
                "WHERE pm.project_id=ws.project_id AND pm.user_id=%s)"
            )
            activity_clause = (
                "AND EXISTS (SELECT 1 FROM project_members pm "
                "WHERE pm.project_id=a.project_id AND pm.user_id=%s)"
            )
            project_clause = (
                "WHERE EXISTS (SELECT 1 FROM project_members pm "
                "WHERE pm.project_id=p.id AND pm.user_id=%s)"
            )
            scope_params = (project_member_id,)
        else:
            session_clause = activity_clause = project_clause = ""
            scope_params = ()
        with self.connect() as connection:
            totals = connection.execute(
                f"""SELECT
                    COALESCE(SUM(EXTRACT(EPOCH FROM (
                        LEAST(COALESCE(s.ended_at, CURRENT_TIMESTAMP), CURRENT_TIMESTAMP)
                        - GREATEST(s.started_at, date_trunc('week', CURRENT_TIMESTAMP))
                    ))), 0)::BIGINT tracked_seconds,
                    COUNT(DISTINCT CASE WHEN ws.status = 'active' THEN ws.user_id END) active_members,
                    COUNT(DISTINCT ws.user_id) tracked_members
                  FROM work_session_segments s
                  JOIN work_sessions ws ON ws.id = s.session_id
                  WHERE s.started_at < CURRENT_TIMESTAMP
                    AND COALESCE(s.ended_at, CURRENT_TIMESTAMP) > date_trunc('week', CURRENT_TIMESTAMP)
                    {session_clause}""",
                scope_params,
            ).fetchone()
            activity = connection.execute(
                f"""SELECT COALESCE(ROUND(AVG(a.activity_percent)), 0)::INTEGER activity_percent,
                           COUNT(*) screenshot_count,
                           COUNT(*) FILTER (WHERE a.automation_suspected) anomaly_count
                    FROM activity_records a
                    WHERE a.captured_at >= date_trunc('week', CURRENT_TIMESTAMP)
                      {activity_clause}""",
                scope_params,
            ).fetchone()
            pending = (
                connection.execute(
                    """SELECT
                         (SELECT COUNT(*) FROM timesheets WHERE status='submitted') timesheets,
                         (SELECT COUNT(*) FROM manual_time_entries WHERE status='pending') manual_time,
                         (SELECT COUNT(*) FROM time_off_requests WHERE status='pending') time_off,
                         (SELECT COUNT(*) FROM expenses WHERE status='pending') expenses"""
                ).fetchone()
                if not scope_params
                else {"timesheets": 0, "manual_time": 0, "time_off": 0, "expenses": 0}
            )
            projects = connection.execute(
                f"""SELECT p.id, p.name, p.color, p.budget_amount, p.budget_minutes,
                          p.budget_type, COALESCE(pm.member_count, 0)::BIGINT member_count,
                          COALESCE(tr.tracked_seconds, 0)::BIGINT tracked_seconds
                   FROM projects p
                   LEFT JOIN LATERAL (
                       SELECT COUNT(*) member_count
                         FROM project_members
                        WHERE project_id = p.id
                   ) pm ON TRUE
                   LEFT JOIN LATERAL (
                       SELECT SUM(EXTRACT(EPOCH FROM (
                                  LEAST(COALESCE(seg.ended_at, CURRENT_TIMESTAMP), CURRENT_TIMESTAMP)
                                  - GREATEST(seg.started_at, date_trunc('week', CURRENT_TIMESTAMP))
                              ))) tracked_seconds
                         FROM work_sessions ws
                         JOIN work_session_segments seg ON seg.session_id = ws.id
                        WHERE ws.project_id = p.id
                          AND seg.started_at < CURRENT_TIMESTAMP
                          AND COALESCE(seg.ended_at, CURRENT_TIMESTAMP) > date_trunc('week', CURRENT_TIMESTAMP)
                   ) tr ON TRUE
                   {project_clause}
                   {"AND" if project_clause else "WHERE"} p.enabled=TRUE
                   ORDER BY tracked_seconds DESC LIMIT 6""",
                scope_params,
            ).fetchall()
            # Seven-day series for the dashboard chart. generate_series keeps
            # days with no tracked time in the result so the bars stay evenly
            # spaced instead of collapsing the gaps.
            daily = connection.execute(
                f"""SELECT d::DATE AS day,
                           COALESCE(t.tracked_seconds, 0)::BIGINT tracked_seconds
                    FROM generate_series(
                             date_trunc('day', CURRENT_TIMESTAMP) - INTERVAL '6 days',
                             date_trunc('day', CURRENT_TIMESTAMP),
                             INTERVAL '1 day') d
                    LEFT JOIN LATERAL (
                        SELECT SUM(EXTRACT(EPOCH FROM (
                                   LEAST(COALESCE(s.ended_at, CURRENT_TIMESTAMP),
                                         d + INTERVAL '1 day')
                                   - GREATEST(s.started_at, d)
                               ))) tracked_seconds
                          FROM work_session_segments s
                          JOIN work_sessions ws ON ws.id = s.session_id
                         WHERE s.started_at < d + INTERVAL '1 day'
                           AND COALESCE(s.ended_at, CURRENT_TIMESTAMP) > d
                           {session_clause}
                    ) t ON TRUE
                    ORDER BY d""",
                scope_params,
            ).fetchall()
            recent = connection.execute(
                f"""SELECT ws.id, u.email, u.full_name, p.name project_name, t.name task_name,
                          ws.status, ws.started_at,
                          COALESCE(SUM(EXTRACT(EPOCH FROM (COALESCE(seg.ended_at,CURRENT_TIMESTAMP)-seg.started_at))),0)::BIGINT tracked_seconds
                   FROM work_sessions ws LEFT JOIN users u ON u.id=ws.user_id
                   LEFT JOIN projects p ON p.id=ws.project_id LEFT JOIN tasks t ON t.id=ws.task_id
                   LEFT JOIN work_session_segments seg ON seg.session_id=ws.id
                   WHERE TRUE {session_clause}
                   GROUP BY ws.id,u.id,p.id,t.id ORDER BY ws.started_at DESC LIMIT 8""",
                scope_params,
            ).fetchall()
        return {
            **dict(totals),
            **dict(activity),
            "pending": dict(pending),
            "daily": [dict(row) for row in daily],
            "projects": [dict(row) for row in projects],
            "recent": [dict(row) for row in recent],
        }

    def activity_feed(
        self,
        user_id: str | None = None,
        project_id: str | None = None,
        project_member_id: str | None = None,
        project_visibility_user_id: str | None = None,
        limit: int = 120,
        captured_from: datetime | None = None,
        captured_to: datetime | None = None,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if user_id:
            conditions.append("a.user_id=%s")
            params.append(user_id)
        if project_id:
            conditions.append("a.project_id=%s")
            params.append(project_id)
        if project_member_id:
            conditions.append(
                "EXISTS (SELECT 1 FROM project_members pm "
                "WHERE pm.project_id=a.project_id AND pm.user_id=%s)"
            )
            params.append(project_member_id)
        if project_visibility_user_id:
            conditions.append(
                "(a.user_id=%s OR EXISTS (SELECT 1 FROM project_members pm "
                "WHERE pm.project_id=a.project_id AND pm.user_id=%s "
                "AND pm.project_role IN ('manager','viewer')))"
            )
            params.extend((project_visibility_user_id, project_visibility_user_id))
        if captured_from:
            conditions.append("a.captured_at >= %s")
            params.append(captured_from)
        if captured_to:
            conditions.append("a.captured_at < %s")
            params.append(captured_to)
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        params.append(min(max(limit, 1), 2_000))
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT a.*,u.email,u.full_name,p.name project_name,p.color project_color,
                           t.name task_name,d.name device_name
                    FROM activity_records a LEFT JOIN users u ON u.id=a.user_id
                    LEFT JOIN projects p ON p.id=a.project_id LEFT JOIN tasks t ON t.id=a.task_id
                    JOIN devices d ON d.id=a.device_id {where}
                    ORDER BY a.captured_at DESC LIMIT %s""",
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    def usage_summary(
        self,
        column: str,
        user_id: str | None = None,
        project_member_id: str | None = None,
        project_visibility_user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if column not in {"active_app", "active_url"}:
            raise ValueError("Unsupported usage dimension")
        where = "WHERE sample.name IS NOT NULL AND sample.name <> ''"
        params: tuple[Any, ...] = ()
        if user_id:
            where += " AND sample.user_id=%s"
            params += (user_id,)
        if project_member_id:
            where += (
                " AND EXISTS (SELECT 1 FROM project_members pm "
                "WHERE pm.project_id=sample.project_id AND pm.user_id=%s)"
            )
            params += (project_member_id,)
        if project_visibility_user_id:
            where += (
                " AND (sample.user_id=%s OR EXISTS (SELECT 1 FROM project_members pm "
                "WHERE pm.project_id=sample.project_id AND pm.user_id=%s "
                "AND pm.project_role IN ('manager','viewer')))"
            )
            params += (project_visibility_user_id, project_visibility_user_id)
        with self.connect() as connection:
            rows = connection.execute(
                f"""WITH sample AS (
                      SELECT u.{column} name,u.focused_seconds,u.user_id,u.project_id
                      FROM usage_records u
                      UNION ALL
                      SELECT a.{column} name,a.focused_seconds,a.user_id,a.project_id
                      FROM activity_records a
                      WHERE NOT EXISTS (
                        SELECT 1 FROM usage_records u
                        WHERE u.device_id=a.device_id AND u.observed_at::date=a.captured_at::date
                      )
                    )
                    SELECT sample.name,COUNT(*) sessions,
                           COALESCE(SUM(sample.focused_seconds),0)::BIGINT seconds,
                           COUNT(DISTINCT sample.user_id) members
                    FROM sample {where} GROUP BY sample.name
                    ORDER BY seconds DESC, name LIMIT 100""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def add_manual_time(
        self,
        user_id: str,
        project_id: str,
        task_id: str | None,
        started_at: datetime,
        ended_at: datetime,
        note: str,
        auto_approve: bool = False,
    ) -> str:
        if ended_at <= started_at:
            raise ValueError("End time must be after start time")
        entry_id = str(uuid.uuid4())
        with self.connect() as connection:
            if connection.execute(
                """SELECT 1 FROM timesheets WHERE user_id=%s AND status='approved'
                   AND daterange(period_start,period_end,'[]')
                       && daterange(%s,%s,'[]') LIMIT 1""",
                (user_id, started_at.date(), ended_at.date()),
            ).fetchone():
                raise ValueError("Approved timesheets are locked")
            connection.execute(
                """INSERT INTO manual_time_entries(id,user_id,project_id,task_id,started_at,ended_at,note,status,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    entry_id,
                    user_id,
                    project_id,
                    task_id or None,
                    started_at,
                    ended_at,
                    note.strip()[:500],
                    "approved" if auto_approve else "pending",
                    utc_now(),
                ),
            )
        return entry_id

    def list_manual_time(
        self, user_id: str | None = None, *, user_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        if user_id and user_ids is not None:
            raise ValueError("Choose one manual-time user scope")
        if user_id:
            where, params = "WHERE m.user_id=%s", (user_id,)
        elif user_ids is not None:
            where, params = "WHERE m.user_id=ANY(%s::uuid[])", (user_ids,)
        else:
            where, params = "", ()
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT m.*,u.email,u.full_name,p.name project_name,t.name task_name,
                           EXTRACT(EPOCH FROM (m.ended_at-m.started_at))::BIGINT seconds
                    FROM manual_time_entries m JOIN users u ON u.id=m.user_id
                    JOIN projects p ON p.id=m.project_id LEFT JOIN tasks t ON t.id=m.task_id
                    {where} ORDER BY m.started_at DESC""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def review_item(
        self, table: str, item_id: str, reviewer_id: str, status: str
    ) -> None:
        allowed = {
            "manual_time_entries": {"approved", "rejected"},
            "time_off_requests": {"approved", "denied"},
            "expenses": {"approved", "rejected", "reimbursed"},
        }
        if table not in allowed or status not in allowed[table]:
            raise ValueError("Unsupported review decision")
        with self.connect() as connection:
            selected_fields = (
                "user_id,started_at::date AS work_date"
                if table == "manual_time_entries"
                else "user_id"
            )
            item = connection.execute(
                f"SELECT {selected_fields} FROM {table} WHERE id=%s FOR UPDATE",
                (item_id,),
            ).fetchone()
            if not item:
                raise ValueError("Item not found")
            if (
                table == "manual_time_entries"
                and connection.execute(
                    """SELECT 1 FROM timesheets WHERE user_id=%s
                   AND status='approved' AND %s BETWEEN period_start AND period_end""",
                    (item["user_id"], item["work_date"]),
                ).fetchone()
            ):
                raise ValueError("Approved timesheets are locked")
            result = connection.execute(
                f"UPDATE {table} SET status=%s,reviewed_by_user_id=%s,reviewed_at=%s WHERE id=%s",
                (status, reviewer_id, utc_now(), item_id),
            )
            if result.rowcount != 1:
                raise ValueError("Item not found")
            connection.execute(
                """INSERT INTO notifications(id,user_id,kind,title,body,created_at)
                   VALUES (%s,%s,'approval',%s,%s,%s)""",
                (
                    str(uuid.uuid4()),
                    item["user_id"],
                    f"{table.replace('_', ' ').title()} {status}",
                    "Your request was reviewed by a manager.",
                    utc_now(),
                ),
            )

    def review_item_owner(self, table: str, item_id: str) -> str | None:
        if table not in {"manual_time_entries", "time_off_requests", "expenses"}:
            raise ValueError("Unsupported review item")
        with self.connect() as connection:
            row = connection.execute(
                f"SELECT user_id FROM {table} WHERE id=%s", (item_id,)
            ).fetchone()
        return row["user_id"] if row else None

    def add_shift(
        self,
        user_id: str,
        project_id: str | None,
        starts_at: datetime,
        ends_at: datetime,
        notes: str,
        creator_id: str,
    ) -> str:
        if ends_at <= starts_at:
            raise ValueError("Shift must end after it starts")
        item_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO shifts(id,user_id,project_id,starts_at,ends_at,notes,created_by_user_id,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    item_id,
                    user_id,
                    project_id or None,
                    starts_at,
                    ends_at,
                    notes.strip()[:500],
                    creator_id,
                    utc_now(),
                ),
            )
        return item_id

    def list_shifts(
        self, user_id: str | None = None, *, user_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        if user_id and user_ids is not None:
            raise ValueError("Choose one shift user scope")
        if user_id:
            where, params = "WHERE s.user_id=%s", (user_id,)
        elif user_ids is not None:
            where, params = "WHERE s.user_id=ANY(%s::uuid[])", (user_ids,)
        else:
            where, params = "", ()
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT s.*,u.email,u.full_name,p.name project_name
                    FROM shifts s JOIN users u ON u.id=s.user_id LEFT JOIN projects p ON p.id=s.project_id
                    {where} ORDER BY s.starts_at DESC LIMIT 200""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def create_holiday(self, name: str, holiday_date: date, paid_minutes: int) -> str:
        holiday_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO holidays(id,name,holiday_date,paid_minutes,created_at)
                   VALUES (%s,%s,%s,%s,%s)""",
                (
                    holiday_id,
                    name.strip()[:120],
                    holiday_date,
                    max(0, paid_minutes),
                    utc_now(),
                ),
            )
        return holiday_id

    def list_holidays(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM holidays ORDER BY holiday_date DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def start_break(self, user_id: str, session_id: str, paid: bool = False) -> str:
        break_id = str(uuid.uuid4())
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM work_breaks WHERE user_id=%s AND ended_at IS NULL",
                (user_id,),
            ).fetchone()
            if existing:
                return existing["id"]
            connection.execute(
                """INSERT INTO work_breaks(id,user_id,session_id,started_at,paid)
                   VALUES (%s,%s,%s,%s,%s)""",
                (break_id, user_id, session_id, utc_now(), paid),
            )
        return break_id

    def stop_break(self, user_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE work_breaks SET ended_at=%s
                   WHERE user_id=%s AND ended_at IS NULL""",
                (utc_now(), user_id),
            )

    def add_time_off(
        self,
        user_id: str,
        category: str,
        starts_on: date,
        ends_on: date,
        minutes: int,
        reason: str,
    ) -> str:
        if ends_on < starts_on:
            raise ValueError("End date must be on or after start date")
        item_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO time_off_requests(id,user_id,category,starts_on,ends_on,minutes,reason,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    item_id,
                    user_id,
                    category[:40],
                    starts_on,
                    ends_on,
                    max(0, minutes),
                    reason.strip()[:500],
                    utc_now(),
                ),
            )
        return item_id

    def list_time_off(
        self, user_id: str | None = None, *, user_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        if user_id and user_ids is not None:
            raise ValueError("Choose one time-off user scope")
        if user_id:
            where, params = "WHERE r.user_id=%s", (user_id,)
        elif user_ids is not None:
            where, params = "WHERE r.user_id=ANY(%s::uuid[])", (user_ids,)
        else:
            where, params = "", ()
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT r.*,u.email,u.full_name FROM time_off_requests r JOIN users u ON u.id=r.user_id {where} ORDER BY r.starts_on DESC",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def add_expense(
        self,
        user_id: str,
        project_id: str | None,
        incurred_on: date,
        category: str,
        amount: Decimal,
        currency: str,
        description: str,
    ) -> str:
        if amount < 0:
            raise ValueError("Amount cannot be negative")
        item_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO expenses(id,user_id,project_id,incurred_on,category,amount,currency,description,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    item_id,
                    user_id,
                    project_id or None,
                    incurred_on,
                    category[:60],
                    amount,
                    currency[:3].upper(),
                    description.strip()[:500],
                    utc_now(),
                ),
            )
        return item_id

    def list_expenses(
        self,
        user_id: str | None = None,
        *,
        user_ids: list[str] | None = None,
        incurred_from: date | None = None,
        incurred_to: date | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if user_id and user_ids is not None:
            raise ValueError("Choose one expense user scope")
        if (incurred_from is None) != (incurred_to is None):
            raise ValueError("Both expense report bounds are required")
        if incurred_from is not None and incurred_from >= incurred_to:
            raise ValueError("Expense report bounds are invalid")
        if limit is not None and not 1 <= limit <= 100_001:
            raise ValueError("Expense report limit is invalid")
        conditions: list[str] = []
        parameters: list[Any] = []
        if user_id:
            conditions.append("e.user_id=%s")
            parameters.append(user_id)
        elif user_ids is not None:
            conditions.append("e.user_id=ANY(%s::uuid[])")
            parameters.append(user_ids)
        if incurred_from is not None:
            conditions.extend(["e.incurred_on >= %s", "e.incurred_on < %s"])
            parameters.extend([incurred_from, incurred_to])
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        limit_sql = "LIMIT %s" if limit is not None else ""
        if limit is not None:
            parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT e.*,u.email,u.full_name,p.name project_name FROM expenses e
                    JOIN users u ON u.id=e.user_id LEFT JOIN projects p ON p.id=e.project_id
                    {where} ORDER BY e.incurred_on DESC,e.id
                    {limit_sql}""",
                tuple(parameters),
            ).fetchall()
        return [dict(row) for row in rows]

    def organization_settings(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM organization_settings WHERE id=1"
            ).fetchone()
        return dict(row)

    @staticmethod
    def _stop_web_timer_sessions(connection, user_id: str | None = None) -> int:
        """Close every non-desktop session when desktop-only is enabled."""
        observed_at = utc_now()
        user_clause = "AND ws.user_id=%s" if user_id else ""
        parameters = (user_id,) if user_id else ()
        session_ids = f"""SELECT ws.id FROM work_sessions ws
                            JOIN devices d ON d.id=ws.device_id
                            CROSS JOIN organization_settings organization
                            LEFT JOIN user_tracking_settings member
                              ON member.user_id=ws.user_id
                            WHERE d.tracker_kind<>'desktop' AND ws.ended_at IS NULL
                              AND organization.id=1
                              AND COALESCE(member.allowed_apps,
                                           organization.allowed_apps)='desktop_only'
                              {user_clause}"""
        connection.execute(
            f"""UPDATE work_session_segments SET ended_at=%s
                WHERE ended_at IS NULL AND session_id IN ({session_ids})""",
            (observed_at,) + parameters,
        )
        connection.execute(
            f"""UPDATE work_breaks SET ended_at=%s
                WHERE ended_at IS NULL AND session_id IN ({session_ids})""",
            (observed_at,) + parameters,
        )
        result = connection.execute(
            f"""UPDATE work_sessions
                SET status='stopped',ended_at=%s,updated_at=%s
                WHERE id IN ({session_ids})""",
            (observed_at, observed_at) + parameters,
        )
        return max(0, result.rowcount)

    def effective_tracking_settings(self, user_id: str | None) -> dict[str, Any]:
        """Return the policy enforced for one worker after default inheritance."""
        fields = ",".join(
            f"COALESCE(member.{field},organization.{field}) AS {field}"
            for field in self.TRACKING_OVERRIDE_FIELDS
        )
        with self.connect() as connection:
            row = connection.execute(
                f"""SELECT {fields}
                    FROM organization_settings organization
                    LEFT JOIN user_tracking_settings member
                      ON member.user_id=%s
                    WHERE organization.id=1""",
                (user_id,),
            ).fetchone()
        return dict(row)

    def list_member_tracking_settings(
        self, search: str = "", *, limit: int = 50, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        """List active workers and both their inherited and effective policies."""
        bounded_limit = min(max(limit, 1), 100)
        bounded_offset = min(max(offset, 0), 1_000_000)
        search_value = search.strip().casefold()[:120]
        where = "u.enabled=TRUE AND u.role IN ('admin','manager','member')"
        parameters: list[Any] = []
        if search_value:
            where += " AND (lower(u.email) LIKE %s OR lower(u.full_name) LIKE %s)"
            prefix = f"{search_value}%"
            parameters.extend((prefix, prefix))
        override_fields = ",".join(
            f"member.{field} AS override_{field}"
            for field in self.TRACKING_OVERRIDE_FIELDS
        )
        effective_fields = ",".join(
            f"COALESCE(member.{field},organization.{field}) AS {field}"
            for field in self.TRACKING_OVERRIDE_FIELDS
        )
        with self.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) AS total FROM users u WHERE {where}",
                tuple(parameters),
            ).fetchone()["total"]
            rows = connection.execute(
                f"""SELECT u.id,u.email,u.full_name,u.role,
                           {override_fields},{effective_fields},member.updated_at
                    FROM users u
                    CROSS JOIN organization_settings organization
                    LEFT JOIN user_tracking_settings member ON member.user_id=u.id
                    WHERE organization.id=1 AND {where}
                    ORDER BY lower(COALESCE(NULLIF(u.full_name,''),u.email)),u.id
                    LIMIT %s OFFSET %s""",
                tuple(parameters) + (bounded_limit, bounded_offset),
            ).fetchall()
        return [dict(row) for row in rows], int(total)

    def update_member_tracking_settings(
        self,
        user_id: str,
        values: dict[str, Any],
        updated_by_user_id: str,
    ) -> None:
        """Replace a member's overrides; ``None`` means inherit organization policy."""
        unknown = set(values) - set(self.TRACKING_OVERRIDE_FIELDS)
        if unknown:
            raise ValueError("Unknown member tracking setting")
        normalized = {
            field: values.get(field) for field in self.TRACKING_OVERRIDE_FIELDS
        }
        frequency = normalized["screenshot_frequency"]
        if frequency is not None and (
            isinstance(frequency, bool)
            or not isinstance(frequency, int)
            or frequency not in range(4)
        ):
            raise ValueError("Screenshot frequency must be between 0 and 3")
        idle_timeout = normalized["idle_timeout_minutes"]
        if idle_timeout is not None and (
            isinstance(idle_timeout, bool)
            or not isinstance(idle_timeout, int)
            or not 1 <= idle_timeout <= 1440
        ):
            raise ValueError("Idle timeout must be between 1 and 1440 minutes")
        for field in (
            "screenshot_blur",
            "track_apps",
            "track_urls",
            "allow_screenshot_delete",
        ):
            if normalized[field] is not None and not isinstance(
                normalized[field], bool
            ):
                raise ValueError("Tracking switches must be on, off, or inherited")
        allowed_apps = normalized["allowed_apps"]
        if allowed_apps not in {None, "all", "desktop_only"}:
            raise ValueError("Allowed apps must be all, desktop only, or inherited")
        with self.connect() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock_shared(hashtext(%s))",
                ("dayfinch:allowed-apps:global",),
            )
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"dayfinch:allowed-apps:{user_id}",),
            )
            target = connection.execute(
                "SELECT role,enabled FROM users WHERE id=%s", (user_id,)
            ).fetchone()
            if not target or not target["enabled"] or target["role"] == "viewer":
                raise ValueError("Active trackable member not found")
            if all(value is None for value in normalized.values()):
                connection.execute(
                    "DELETE FROM user_tracking_settings WHERE user_id=%s", (user_id,)
                )
                allowed_apps = connection.execute(
                    "SELECT allowed_apps FROM organization_settings WHERE id=1"
                ).fetchone()["allowed_apps"]
                if allowed_apps == "desktop_only":
                    self._stop_web_timer_sessions(connection, user_id)
                return
            columns = ",".join(self.TRACKING_OVERRIDE_FIELDS)
            placeholders = ",".join("%s" for _ in self.TRACKING_OVERRIDE_FIELDS)
            updates = ",".join(
                f"{field}=EXCLUDED.{field}" for field in self.TRACKING_OVERRIDE_FIELDS
            )
            connection.execute(
                f"""INSERT INTO user_tracking_settings(
                           user_id,{columns},updated_at,updated_by_user_id
                       ) VALUES (%s,{placeholders},%s,%s)
                       ON CONFLICT(user_id) DO UPDATE SET
                           {updates},updated_at=EXCLUDED.updated_at,
                           updated_by_user_id=EXCLUDED.updated_by_user_id""",
                (user_id,)
                + tuple(normalized[field] for field in self.TRACKING_OVERRIDE_FIELDS)
                + (utc_now(), updated_by_user_id),
            )
            effective_allowed_apps = (
                normalized["allowed_apps"]
                or connection.execute(
                    "SELECT allowed_apps FROM organization_settings WHERE id=1"
                ).fetchone()["allowed_apps"]
            )
            if effective_allowed_apps == "desktop_only":
                self._stop_web_timer_sessions(connection, user_id)

    def update_organization_settings(self, values: dict[str, Any]) -> None:
        if values.get("allowed_apps", "all") not in {"all", "desktop_only"}:
            raise ValueError("Allowed apps must be all or desktop only")
        try:
            weekly_overtime_minutes = int(values.get("weekly_overtime_minutes", 2400))
            multiplier = Decimal(str(values.get("overtime_multiplier", "1.5")))
        except (ValueError, InvalidOperation) as exc:
            raise ValueError("Overtime settings are invalid") from exc
        if not 0 <= weekly_overtime_minutes <= 10080:
            raise ValueError("Weekly overtime threshold is invalid")
        if not Decimal("1") <= multiplier <= Decimal("10"):
            raise ValueError("Overtime multiplier is invalid")
        fields = (
            "name",
            "address",
            "tax_id",
            "timezone",
            "currency",
            "screenshot_frequency",
            "screenshot_blur",
            "track_apps",
            "track_urls",
            "allowed_apps",
            "allow_manual_time",
            "require_time_approval",
            "allow_screenshot_delete",
            "require_edit_reason",
            "allow_keep_idle",
            "pay_period",
            "overtime_enabled",
            "weekly_overtime_minutes",
            "overtime_multiplier",
            "require_two_factor",
            "sso_provider",
            "sso_domain",
            "idle_timeout_minutes",
            "retention_days",
        )
        assignments = ",".join(f"{field}=%s" for field in fields)
        with self.connect() as connection:
            if "allowed_apps" in values:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    ("dayfinch:allowed-apps:global",),
                )
            current = connection.execute(
                "SELECT * FROM organization_settings WHERE id=1"
            ).fetchone()
            connection.execute(
                f"UPDATE organization_settings SET {assignments},updated_at=%s WHERE id=1",
                tuple(values.get(field, current[field]) for field in fields)
                + (utc_now(),),
            )
            if values.get("allowed_apps", current["allowed_apps"]) == "desktop_only":
                self._stop_web_timer_sessions(connection)

    def set_user_profile(
        self,
        user_id: str,
        role: str,
        full_name: str,
        pay_rate: Decimal,
        bill_rate: Decimal,
        weekly_limit_minutes: int,
        daily_limit_minutes: int | None = None,
        manage_it: bool = False,
    ) -> None:
        if role not in {"admin", "manager", "member"}:
            raise ValueError("Invalid role")
        with self.connect() as connection:
            connection.execute(
                """UPDATE users SET role=%s,full_name=%s,pay_rate=%s,bill_rate=%s,weekly_limit_minutes=%s,
                   daily_limit_minutes=COALESCE(%s,daily_limit_minutes),manage_it=%s
                   WHERE id=%s""",
                (
                    role,
                    full_name.strip()[:120],
                    pay_rate,
                    bill_rate,
                    max(0, weekly_limit_minutes),
                    max(0, daily_limit_minutes)
                    if daily_limit_minutes is not None
                    else None,
                    manage_it,
                    user_id,
                ),
            )

    def finance_summary(self, *, include_payroll: bool = True) -> dict[str, Any]:
        payroll_total = (
            "COALESCE((SELECT SUM(gross_amount) FROM payroll_payments WHERE status='paid'),0)"
            if include_payroll
            else "0"
        )
        with self.connect() as connection:
            totals = connection.execute(
                f"""SELECT
                  COALESCE((SELECT SUM(amount) FROM expenses WHERE status IN ('approved','reimbursed')),0) expenses,
                  {payroll_total} payroll,
                  COALESCE((SELECT SUM(il.quantity*il.unit_price) FROM invoice_lines il JOIN invoices i ON i.id=il.invoice_id WHERE i.status='paid'),0) received,
                  COALESCE((SELECT SUM(il.quantity*il.unit_price) FROM invoice_lines il JOIN invoices i ON i.id=il.invoice_id WHERE i.status IN ('sent','overdue')),0) outstanding"""
            ).fetchone()
            invoices = connection.execute(
                """SELECT i.*,c.name client_name,COALESCE(SUM(il.quantity*il.unit_price),0) subtotal
                   FROM invoices i LEFT JOIN clients c ON c.id=i.client_id LEFT JOIN invoice_lines il ON il.invoice_id=i.id
                   GROUP BY i.id,c.id ORDER BY i.issued_on DESC"""
            ).fetchall()
            payroll = (
                connection.execute(
                    """SELECT pp.*,u.email,u.full_name FROM payroll_payments pp JOIN users u ON u.id=pp.user_id ORDER BY pp.period_start DESC"""
                ).fetchall()
                if include_payroll
                else []
            )
            clients = connection.execute(
                "SELECT * FROM clients ORDER BY lower(name)"
            ).fetchall()
        return {
            **dict(totals),
            "invoices": [dict(x) for x in invoices],
            # `totals` already carries a scalar `payroll` (gross paid), so the
            # run list needs its own key or it overwrites that total.
            "payroll_runs": [dict(x) for x in payroll],
            "clients": [dict(x) for x in clients],
        }

    def create_client(self, name: str, email: str, address: str = "") -> str:
        client_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO clients(id,name,email,address,created_at)
                   VALUES (%s,%s,%s,%s,%s)""",
                (
                    client_id,
                    name.strip()[:120],
                    email.strip()[:254],
                    address.strip()[:500],
                    utc_now(),
                ),
            )
        return client_id

    def create_invoice(
        self,
        client_id: str,
        issued_on: date,
        due_on: date,
        description: str,
        quantity: Decimal,
        unit_price: Decimal,
        currency: str,
        creator_id: str,
    ) -> str:
        if due_on < issued_on or quantity <= 0 or unit_price < 0:
            raise ValueError("Invoice dates, quantity, or rate are invalid")
        invoice_id, line_id = str(uuid.uuid4()), str(uuid.uuid4())
        with self.connect() as connection:
            sequence = connection.execute(
                "SELECT COUNT(*) + 1 value FROM invoices WHERE issued_on >= date_trunc('year', %s::date)",
                (issued_on,),
            ).fetchone()["value"]
            number = f"DF-{issued_on.year}-{sequence:04d}"
            connection.execute(
                """INSERT INTO invoices(id,number,client_id,issued_on,due_on,currency,created_by_user_id,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    invoice_id,
                    number,
                    client_id,
                    issued_on,
                    due_on,
                    currency[:3].upper(),
                    creator_id,
                    utc_now(),
                ),
            )
            connection.execute(
                """INSERT INTO invoice_lines(id,invoice_id,description,quantity,unit_price)
                   VALUES (%s,%s,%s,%s,%s)""",
                (line_id, invoice_id, description.strip()[:500], quantity, unit_price),
            )
        return invoice_id

    def invoice_snapshot(self, invoice_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            invoice = connection.execute(
                """SELECT i.*,c.name client_name,c.email client_email,
                          c.address client_address
                   FROM invoices i LEFT JOIN clients c ON c.id=i.client_id
                   WHERE i.id=%s""",
                (invoice_id,),
            ).fetchone()
            if not invoice:
                return None
            lines = connection.execute(
                """SELECT il.*,p.name project_name FROM invoice_lines il
                   LEFT JOIN projects p ON p.id=il.project_id
                   WHERE il.invoice_id=%s ORDER BY il.id""",
                (invoice_id,),
            ).fetchall()
        result = dict(invoice)
        result["lines"] = [
            {
                **dict(line),
                "line_total": line["quantity"] * line["unit_price"],
            }
            for line in lines
        ]
        result["subtotal"] = sum(
            (line["quantity"] * line["unit_price"] for line in lines), Decimal(0)
        )
        return result

    def create_team_invoice(
        self,
        user_id: str,
        issued_on: date,
        due_on: date,
        description: str,
        quantity: Decimal,
        unit_price: Decimal,
        currency: str,
        purchase_order: str = "",
        notes: str = "",
    ) -> str:
        if due_on < issued_on or quantity <= 0 or unit_price < 0:
            raise ValueError("Invoice dates, quantity, or rate are invalid")
        if not description.strip():
            raise ValueError("Invoice line description is required")
        invoice_id, line_id = str(uuid.uuid4()), str(uuid.uuid4())
        try:
            with self.connect() as connection:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"team-invoice:{user_id}:{issued_on.year}",),
                )
                owner = connection.execute(
                    "SELECT email,full_name FROM users WHERE id=%s AND enabled=TRUE",
                    (user_id,),
                ).fetchone()
                if not owner:
                    raise ValueError("Invoice owner not found")
                sequence = connection.execute(
                    """SELECT COUNT(*) + 1 value FROM team_invoices
                       WHERE user_id=%s AND issued_on >= make_date(%s,1,1)
                         AND issued_on < make_date(%s,1,1)""",
                    (user_id, issued_on.year, issued_on.year + 1),
                ).fetchone()["value"]
                label = owner["full_name"] or owner["email"].split("@", 1)[0]
                initials = (
                    "".join(part[0] for part in label.upper().split() if part)[:4]
                    or "DF"
                )
                number = (
                    f"{initials}-{issued_on.year}-{str(user_id)[:6].upper()}-"
                    f"{sequence:04d}"
                )
                now = utc_now()
                connection.execute(
                    """INSERT INTO team_invoices(
                           id,number,user_id,issued_on,due_on,currency,purchase_order,
                           notes,created_at,updated_at
                       ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        invoice_id,
                        number,
                        user_id,
                        issued_on,
                        due_on,
                        currency[:3].upper(),
                        purchase_order.strip()[:120],
                        notes.strip()[:1000],
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """INSERT INTO team_invoice_lines(
                           id,invoice_id,description,quantity,unit_price,source
                       ) VALUES (%s,%s,%s,%s,%s,'manual')""",
                    (
                        line_id,
                        invoice_id,
                        description.strip()[:500],
                        quantity,
                        unit_price,
                    ),
                )
        except UniqueViolation as exc:
            raise ValueError("Invoice number or time source is already in use") from exc
        return invoice_id

    def create_team_invoice_from_time(
        self,
        user_id: str,
        issued_on: date,
        due_on: date,
        period_start: date,
        period_end: date,
        project_id: str | None,
        currency: str,
        purchase_order: str = "",
        notes: str = "",
    ) -> str:
        if period_end < period_start:
            raise ValueError("Invoice period is invalid")
        with self.connect() as connection:
            owner = connection.execute(
                "SELECT pay_rate FROM users WHERE id=%s AND enabled=TRUE", (user_id,)
            ).fetchone()
            if not owner:
                raise ValueError("Invoice owner not found")
            project_filter = "AND ws.project_id=%s" if project_id else ""
            project_params: tuple[Any, ...] = (project_id,) if project_id else ()
            sources = connection.execute(
                f"""SELECT 'segment' source_type,seg.id source_id,ws.project_id,
                           EXTRACT(EPOCH FROM (seg.ended_at-seg.started_at))::INTEGER seconds
                    FROM work_session_segments seg
                    JOIN work_sessions ws ON ws.id=seg.session_id
                    WHERE ws.user_id=%s AND seg.ended_at IS NOT NULL
                      AND seg.started_at::date BETWEEN %s AND %s {project_filter}
                      AND NOT EXISTS (
                          SELECT 1 FROM team_invoice_time_sources tis
                          WHERE tis.source_type='segment' AND tis.source_id=seg.id)
                    UNION ALL
                    SELECT 'manual',m.id,m.project_id,
                           EXTRACT(EPOCH FROM (m.ended_at-m.started_at))::INTEGER
                    FROM manual_time_entries m
                    WHERE m.user_id=%s AND m.status='approved'
                      AND m.started_at::date BETWEEN %s AND %s
                      {"AND m.project_id=%s" if project_id else ""}
                      AND NOT EXISTS (
                          SELECT 1 FROM team_invoice_time_sources tis
                          WHERE tis.source_type='manual' AND tis.source_id=m.id)""",
                (
                    user_id,
                    period_start,
                    period_end,
                    *project_params,
                    user_id,
                    period_start,
                    period_end,
                    *project_params,
                ),
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for source in sources:
            if source["project_id"] and source["seconds"] > 0:
                grouped.setdefault(str(source["project_id"]), []).append(dict(source))
        if not grouped:
            raise ValueError("No uninvoiced completed time matches this period")
        invoice_id = self.create_team_invoice(
            user_id,
            issued_on,
            due_on,
            "Tracked time",
            Decimal("1"),
            Decimal("0"),
            currency,
            purchase_order,
            notes,
        )
        try:
            with self.connect() as connection:
                connection.execute(
                    "DELETE FROM team_invoice_lines WHERE invoice_id=%s", (invoice_id,)
                )
                for grouped_project_id, project_sources in grouped.items():
                    seconds = sum(source["seconds"] for source in project_sources)
                    quantity = (Decimal(seconds) / Decimal(3600)).quantize(
                        Decimal("0.01")
                    )
                    if quantity <= 0:
                        continue
                    project = connection.execute(
                        "SELECT name FROM projects WHERE id=%s", (grouped_project_id,)
                    ).fetchone()
                    line_id = str(uuid.uuid4())
                    connection.execute(
                        """INSERT INTO team_invoice_lines(
                               id,invoice_id,project_id,description,quantity,unit_price,
                               source,period_start,period_end
                           ) VALUES (%s,%s,%s,%s,%s,%s,'tracked',%s,%s)""",
                        (
                            line_id,
                            invoice_id,
                            grouped_project_id,
                            f"Tracked time — {project['name']}",
                            quantity,
                            owner["pay_rate"],
                            period_start,
                            period_end,
                        ),
                    )
                    for source in project_sources:
                        connection.execute(
                            """INSERT INTO team_invoice_time_sources(
                                   invoice_line_id,source_type,source_id,seconds
                               ) VALUES (%s,%s,%s,%s)""",
                            (
                                line_id,
                                source["source_type"],
                                source["source_id"],
                                source["seconds"],
                            ),
                        )
                count = connection.execute(
                    "SELECT COUNT(*) value FROM team_invoice_lines WHERE invoice_id=%s",
                    (invoice_id,),
                ).fetchone()["value"]
                if not count:
                    raise ValueError("Tracked time is too short to invoice")
        except (UniqueViolation, ValueError) as exc:
            with self.connect() as connection:
                connection.execute(
                    "DELETE FROM team_invoices WHERE id=%s", (invoice_id,)
                )
            if isinstance(exc, UniqueViolation):
                raise ValueError("Some selected time was already invoiced") from exc
            raise
        return invoice_id

    def list_team_invoices(
        self, user_id: str | None = None, user_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        if user_id and user_ids is not None:
            raise ValueError("Choose one team-invoice user scope")
        if user_id:
            where, params = "WHERE ti.user_id=%s", (user_id,)
        elif user_ids is not None:
            where, params = "WHERE ti.user_id=ANY(%s::uuid[])", (user_ids,)
        else:
            where, params = "", ()
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT ti.*,u.email,u.full_name,
                           COALESCE(SUM(til.quantity*til.unit_price),0) total,
                           GREATEST(COALESCE(SUM(til.quantity*til.unit_price),0)
                                    - ti.paid_amount,0) amount_due
                    FROM team_invoices ti JOIN users u ON u.id=ti.user_id
                    LEFT JOIN team_invoice_lines til ON til.invoice_id=ti.id
                    {where} GROUP BY ti.id,u.id
                    ORDER BY ti.issued_on DESC,ti.created_at DESC""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def team_invoice_snapshot(self, invoice_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            invoice = connection.execute(
                """SELECT ti.*,u.email,u.full_name,u.pay_rate
                   FROM team_invoices ti JOIN users u ON u.id=ti.user_id
                   WHERE ti.id=%s""",
                (invoice_id,),
            ).fetchone()
            if not invoice:
                return None
            lines = connection.execute(
                """SELECT til.*,p.name project_name FROM team_invoice_lines til
                   LEFT JOIN projects p ON p.id=til.project_id
                   WHERE til.invoice_id=%s ORDER BY til.id""",
                (invoice_id,),
            ).fetchall()
            payments = connection.execute(
                """SELECT tip.*,u.email recorded_by_email
                   FROM team_invoice_payments tip LEFT JOIN users u
                     ON u.id=tip.recorded_by_user_id
                   WHERE tip.invoice_id=%s ORDER BY tip.created_at""",
                (invoice_id,),
            ).fetchall()
        result = dict(invoice)
        result["lines"] = [
            {
                **dict(line),
                "line_total": line["quantity"] * line["unit_price"],
            }
            for line in lines
        ]
        result["payments"] = [dict(payment) for payment in payments]
        result["total"] = sum(
            (line["quantity"] * line["unit_price"] for line in lines), Decimal(0)
        )
        result["amount_due"] = max(Decimal(0), result["total"] - result["paid_amount"])
        return result

    def submit_team_invoice(self, invoice_id: str, user_id: str) -> None:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE team_invoices SET status='submitted',submitted_at=COALESCE(submitted_at,%s),
                          updated_at=%s
                   WHERE id=%s AND user_id=%s AND status IN ('draft','submitted')
                     AND EXISTS (SELECT 1 FROM team_invoice_lines
                                 WHERE invoice_id=team_invoices.id)""",
                (utc_now(), utc_now(), invoice_id, user_id),
            )
            if result.rowcount != 1:
                raise ValueError("Draft team invoice not found")

    def update_team_invoice_draft(
        self,
        invoice_id: str,
        user_id: str,
        issued_on: date,
        due_on: date,
        purchase_order: str,
        notes: str,
        description: str | None = None,
        quantity: Decimal | None = None,
        unit_price: Decimal | None = None,
    ) -> None:
        if due_on < issued_on:
            raise ValueError("Invoice due date cannot precede its issue date")
        with self.connect() as connection:
            invoice = connection.execute(
                """SELECT id FROM team_invoices
                   WHERE id=%s AND user_id=%s AND status='draft' FOR UPDATE""",
                (invoice_id, user_id),
            ).fetchone()
            if not invoice:
                raise ValueError("Draft team invoice not found")
            if description is not None:
                if not description.strip() or quantity is None or quantity <= 0:
                    raise ValueError(
                        "Manual line description and quantity are required"
                    )
                if unit_price is None or unit_price < 0:
                    raise ValueError("Manual line rate is invalid")
                result = connection.execute(
                    """UPDATE team_invoice_lines SET description=%s,quantity=%s,
                              unit_price=%s
                       WHERE invoice_id=%s AND source='manual'""",
                    (
                        description.strip()[:500],
                        quantity,
                        unit_price,
                        invoice_id,
                    ),
                )
                if result.rowcount != 1:
                    raise ValueError(
                        "Tracked invoice lines cannot be manually replaced"
                    )
            connection.execute(
                """UPDATE team_invoices SET issued_on=%s,due_on=%s,purchase_order=%s,
                          notes=%s,updated_at=%s WHERE id=%s""",
                (
                    issued_on,
                    due_on,
                    purchase_order.strip()[:120],
                    notes.strip()[:1000],
                    utc_now(),
                    invoice_id,
                ),
            )

    def delete_team_invoice_draft(self, invoice_id: str, user_id: str) -> None:
        with self.connect() as connection:
            result = connection.execute(
                "DELETE FROM team_invoices WHERE id=%s AND user_id=%s AND status='draft'",
                (invoice_id, user_id),
            )
            if result.rowcount != 1:
                raise ValueError("Draft team invoice not found")

    def record_team_invoice_document(
        self, invoice_id: str, key: str, version_id: str | None, sha256_digest: str
    ) -> None:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE team_invoices SET encrypted_document_key=%s,
                          document_version_id=%s,encrypted_document_sha256=%s,
                          document_sealed_at=%s WHERE id=%s""",
                (key, version_id, sha256_digest, utc_now(), invoice_id),
            )
            if result.rowcount != 1:
                raise ValueError("Team invoice not found")

    def record_team_invoice_payment(
        self,
        invoice_id: str,
        amount: Decimal,
        paid_on: date,
        reference: str,
        recorder_id: str,
    ) -> str:
        if amount <= 0:
            raise ValueError("Payment amount must be positive")
        payment_id = str(uuid.uuid4())
        with self.connect() as connection:
            invoice = connection.execute(
                "SELECT * FROM team_invoices WHERE id=%s FOR UPDATE", (invoice_id,)
            ).fetchone()
            if not invoice or invoice["status"] not in {"submitted", "partially_paid"}:
                raise ValueError("Payable team invoice not found")
            total = connection.execute(
                """SELECT COALESCE(SUM(quantity*unit_price),0) value
                   FROM team_invoice_lines WHERE invoice_id=%s""",
                (invoice_id,),
            ).fetchone()["value"]
            remaining = total - invoice["paid_amount"]
            if amount > remaining:
                raise ValueError("Payment exceeds the invoice balance")
            new_paid = invoice["paid_amount"] + amount
            connection.execute(
                """INSERT INTO team_invoice_payments(
                       id,invoice_id,amount,paid_on,reference,recorded_by_user_id,created_at
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (
                    payment_id,
                    invoice_id,
                    amount,
                    paid_on,
                    reference.strip()[:200],
                    recorder_id,
                    utc_now(),
                ),
            )
            connection.execute(
                """UPDATE team_invoices SET paid_amount=%s,status=%s,updated_at=%s
                   WHERE id=%s""",
                (
                    new_paid,
                    "paid" if new_paid == total else "partially_paid",
                    utc_now(),
                    invoice_id,
                ),
            )
        return payment_id

    def void_team_invoice(self, invoice_id: str) -> None:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE team_invoices SET status='void',updated_at=%s
                   WHERE id=%s AND status='submitted' AND paid_amount=0""",
                (utc_now(), invoice_id),
            )
            if result.rowcount != 1:
                raise ValueError("Only an unpaid submitted invoice can be voided")

    def record_invoice_document(
        self,
        invoice_id: str,
        key: str,
        version_id: str | None,
        sha256_digest: str,
    ) -> None:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE invoices SET encrypted_document_key=%s,
                   document_version_id=%s,encrypted_document_sha256=%s,
                   document_sealed_at=%s WHERE id=%s""",
                (key, version_id, sha256_digest, utc_now(), invoice_id),
            )
            if result.rowcount != 1:
                raise ValueError("Invoice not found")

    def create_payroll(
        self, user_id: str, period_start: date, period_end: date, currency: str
    ) -> str:
        if period_end < period_start:
            raise ValueError("Payroll period is invalid")
        if (period_end - period_start).days > 31:
            raise ValueError("A payroll period cannot exceed 32 days")
        currency = currency.strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("Payroll currency must be a three-letter code")
        payment_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"dayfinch:payroll:{user_id}",),
            )
            user = connection.execute(
                """SELECT pay_rate,require_timesheet_approval,enabled
                   FROM users WHERE id=%s""",
                (user_id,),
            ).fetchone()
            if not user or not user["enabled"]:
                raise ValueError("Active member not found")
            if user["pay_rate"] <= 0:
                raise ValueError("Set a positive member pay rate before payroll")
            duplicate = connection.execute(
                """SELECT 1 FROM payroll_payments
                   WHERE user_id=%s
                     AND daterange(period_start,period_end,'[]')
                         && daterange(%s,%s,'[]') LIMIT 1""",
                (user_id, period_start, period_end),
            ).fetchone()
            if duplicate:
                raise ValueError("An overlapping payroll run already exists")
            source_timesheet_id = None
            if user["require_timesheet_approval"]:
                timesheet = connection.execute(
                    """SELECT id FROM timesheets WHERE user_id=%s
                       AND period_start=%s AND period_end=%s AND status='approved'""",
                    (user_id, period_start, period_end),
                ).fetchone()
                if not timesheet:
                    raise ValueError(
                        "Approve the matching timesheet before generating payroll"
                    )
                source_timesheet_id = timesheet["id"]
            settings = connection.execute(
                """SELECT timezone,week_starts_on,overtime_enabled,
                          weekly_overtime_minutes,overtime_multiplier
                   FROM organization_settings WHERE id=1"""
            ).fetchone()
            try:
                payroll_timezone = ZoneInfo(settings["timezone"])
            except ZoneInfoNotFoundError as exc:
                raise ValueError("Organization timezone is invalid") from exc
            if period_end >= datetime.now(payroll_timezone).date():
                raise ValueError("Payroll can only be generated for a completed period")
            lower = datetime.combine(period_start, time.min, tzinfo=payroll_timezone)
            upper = datetime.combine(
                period_end + timedelta(days=1), time.min, tzinfo=payroll_timezone
            )
            rows = connection.execute(
                """SELECT seg.started_at,seg.ended_at FROM work_sessions ws
                   JOIN work_session_segments seg ON seg.session_id=ws.id
                   WHERE ws.user_id=%s AND seg.ended_at IS NOT NULL
                     AND seg.started_at < %s AND seg.ended_at > %s
                   UNION ALL
                   SELECT started_at,ended_at FROM manual_time_entries
                   WHERE user_id=%s AND status='approved'
                     AND started_at < %s AND ended_at > %s
                   ORDER BY started_at""",
                (user_id, upper, lower, user_id, upper, lower),
            ).fetchall()
            intervals: list[tuple[datetime, datetime]] = []
            for row in rows:
                started_value = datetime.fromisoformat(row["started_at"])
                ended_value = datetime.fromisoformat(row["ended_at"])
                started_at = max(started_value, lower)
                ended_at = min(ended_value, upper)
                if ended_at <= started_at:
                    continue
                if intervals and started_at < intervals[-1][1]:
                    raise ValueError(
                        "Payroll contains overlapping tracked or manual time"
                    )
                intervals.append((started_at, ended_at))
            week_seconds: dict[date, int] = {}
            for started_at, ended_at in intervals:
                cursor = started_at.astimezone(payroll_timezone)
                local_end = ended_at.astimezone(payroll_timezone)
                while cursor < local_end:
                    next_day = datetime.combine(
                        cursor.date() + timedelta(days=1),
                        time.min,
                        tzinfo=payroll_timezone,
                    )
                    boundary = min(local_end, next_day)
                    seconds = int(
                        (
                            boundary.astimezone(UTC) - cursor.astimezone(UTC)
                        ).total_seconds()
                    )
                    day = cursor.date()
                    configured_weekday = (
                        6
                        if settings["week_starts_on"] == 0
                        else settings["week_starts_on"] - 1
                    )
                    week_start = day - timedelta(
                        days=(day.weekday() - configured_weekday) % 7
                    )
                    week_seconds[week_start] = week_seconds.get(week_start, 0) + seconds
                    cursor = boundary
            if not week_seconds or sum(week_seconds.values()) < 60:
                raise ValueError("The approved period has no payable time")
            regular = 0
            overtime = 0
            threshold = int(settings["weekly_overtime_minutes"]) * 60
            for seconds in week_seconds.values():
                if settings["overtime_enabled"]:
                    regular += min(seconds, threshold) // 60
                    overtime += max(0, seconds - threshold) // 60
                else:
                    regular += seconds // 60
            overtime_multiplier = Decimal(settings["overtime_multiplier"])
            gross = (Decimal(regular) / 60 * user["pay_rate"]) + (
                Decimal(overtime) / 60 * user["pay_rate"] * overtime_multiplier
            )
            connection.execute(
                """INSERT INTO payroll_payments(
                       id,user_id,period_start,period_end,regular_minutes,
                       overtime_minutes,pay_rate_snapshot,
                       overtime_multiplier_snapshot,gross_amount,currency,
                       source_timesheet_id,created_at
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    payment_id,
                    user_id,
                    period_start,
                    period_end,
                    regular,
                    overtime,
                    user["pay_rate"],
                    overtime_multiplier,
                    gross.quantize(Decimal("0.01")),
                    currency,
                    source_timesheet_id,
                    utc_now(),
                ),
            )
        return payment_id

    def set_financial_status(self, table: str, item_id: str, item_status: str) -> None:
        allowed = {
            "invoices": {"draft", "sent", "paid", "void", "overdue"},
            "payroll_payments": {"draft", "processing", "paid", "failed"},
        }
        if table not in allowed or item_status not in allowed[table]:
            raise ValueError("Invalid financial status")
        paid_field = (
            ",paid_at=CURRENT_TIMESTAMP"
            if table == "payroll_payments" and item_status == "paid"
            else ""
        )
        with self.connect() as connection:
            if table == "payroll_payments":
                payroll = connection.execute(
                    "SELECT provider,status FROM payroll_payments WHERE id=%s FOR UPDATE",
                    (item_id,),
                ).fetchone()
                if not payroll:
                    raise ValueError("Financial record not found")
                if payroll["provider"] != "manual":
                    raise ValueError(
                        "Provider-managed payroll status can only be updated by reconciliation"
                    )
                if payroll["status"] == "paid" and item_status != "paid":
                    raise ValueError(
                        "Paid payroll cannot be moved to an earlier status"
                    )
            updated_field = (
                ",updated_at=CURRENT_TIMESTAMP" if table == "payroll_payments" else ""
            )
            result = connection.execute(
                f"UPDATE {table} SET status=%s{paid_field}{updated_field} WHERE id=%s",
                (item_status, item_id),
            )
            if result.rowcount != 1:
                raise ValueError("Financial record not found")

    def get_payroll_payment(self, payment_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT pp.*,u.email,u.full_name FROM payroll_payments pp
                   JOIN users u ON u.id=pp.user_id WHERE pp.id=%s""",
                (payment_id,),
            ).fetchone()
        return dict(row) if row else None

    def queue_wise_payroll_event(
        self,
        transfer_id: str,
        provider_status: str,
        occurred_at: datetime,
        *,
        failure_code: str = "",
        failure_description: str = "",
    ) -> dict[str, Any] | None:
        """Persist a newer Wise event and make canonical reconciliation due now."""

        if occurred_at.tzinfo is None:
            raise ValueError("Wise event time must include a timezone")
        event_at = occurred_at.astimezone(UTC)
        safe_status = provider_status.replace("\x00", "").strip().lower()[:80]
        safe_code = failure_code.replace("\x00", "").strip()[:120]
        safe_description = " ".join(failure_description.replace("\x00", "").split())[
            :500
        ]
        with self.connect() as connection:
            current = connection.execute(
                """SELECT id,status,provider_event_at FROM payroll_payments
                   WHERE provider='wise' AND external_reference=%s
                   FOR UPDATE""",
                (transfer_id,),
            ).fetchone()
            if not current:
                return None
            previous_event_at = (
                datetime.fromisoformat(current["provider_event_at"])
                if current["provider_event_at"]
                else None
            )
            if previous_event_at and event_at <= previous_event_at:
                return {
                    "id": current["id"],
                    "status": current["status"],
                    "outcome": "stale",
                }
            reconcilable = current["status"] in {"processing", "paid", "failed"}
            row = connection.execute(
                """UPDATE payroll_payments
                   SET provider_event_at=%s,provider_status=%s,
                       provider_failure_code=%s,
                       provider_failure_description=%s,
                       next_reconcile_at=CASE WHEN %s THEN CURRENT_TIMESTAMP
                                              ELSE next_reconcile_at END,
                       reconcile_until=CASE
                           WHEN %s AND status='paid' THEN GREATEST(
                               COALESCE(reconcile_until,CURRENT_TIMESTAMP),
                               CURRENT_TIMESTAMP + INTERVAL '90 days'
                           ) ELSE reconcile_until END,
                       reconcile_attempts=CASE WHEN %s THEN 0
                                               ELSE reconcile_attempts END,
                       last_reconcile_error=CASE WHEN %s THEN ''
                                                 ELSE last_reconcile_error END,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=%s RETURNING *""",
                (
                    event_at,
                    safe_status,
                    safe_code,
                    safe_description,
                    reconcilable,
                    reconcilable,
                    reconcilable,
                    reconcilable,
                    current["id"],
                ),
            ).fetchone()
            connection.execute(
                """INSERT INTO audit_events(
                       id,actor_user_id,action,target_type,target_id,
                       occurred_at,details
                   ) VALUES (%s,NULL,%s,'payroll',%s,%s,%s)""",
                (
                    str(uuid.uuid4()),
                    "payroll.wise_event_received",
                    current["id"],
                    utc_now(),
                    f"{safe_status}:{safe_code}"[:500],
                ),
            )
        result = dict(row)
        result["outcome"] = "queued" if reconcilable else "recorded"
        return result

    def set_payroll_destination(
        self,
        user_id: str,
        provider: str,
        recipient: str,
        updated_by_user_id: str,
        currency: str = "",
    ) -> None:
        if provider not in {"paypal", "wise"}:
            raise ValueError("Unsupported payroll destination provider")
        recipient = recipient.strip().lower()
        currency = currency.strip().upper()
        if provider == "paypal" and (
            not recipient
            or len(recipient) > 127
            or recipient.count("@") != 1
            or any(character.isspace() for character in recipient)
        ):
            raise ValueError("A valid payroll recipient is required")
        if provider == "wise" and (
            not recipient.isdigit()
            or int(recipient) <= 0
            or len(currency) != 3
            or not currency.isalpha()
        ):
            raise ValueError(
                "A positive Wise recipient account ID and currency are required"
            )
        with self.connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM users WHERE id=%s AND enabled=TRUE", (user_id,)
            ).fetchone():
                raise ValueError("Active member not found")
            connection.execute(
                """INSERT INTO payroll_destinations(
                       user_id,provider,recipient,currency,confirmed_at,
                       updated_by_user_id,updated_at
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(user_id,provider) DO UPDATE SET
                       recipient=EXCLUDED.recipient,
                       currency=EXCLUDED.currency,
                       confirmed_at=EXCLUDED.confirmed_at,
                       updated_by_user_id=EXCLUDED.updated_by_user_id,
                       updated_at=EXCLUDED.updated_at""",
                (
                    user_id,
                    provider,
                    recipient,
                    currency if provider == "wise" else "",
                    utc_now(),
                    updated_by_user_id,
                    utc_now(),
                ),
            )

    def payroll_destinations(self, provider: str) -> dict[str, dict[str, Any]]:
        if provider not in {"paypal", "wise"}:
            return {}
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM payroll_destinations WHERE provider=%s", (provider,)
            ).fetchall()
        return {row["user_id"]: dict(row) for row in rows}

    def claim_payroll_delivery(self, payment_id: str, provider: str) -> dict[str, Any]:
        """Atomically reserve a payment and freeze its delivery destination."""

        if provider not in {"webhook", "paypal", "wise"}:
            raise ValueError("Unsupported payroll provider")
        with self.connect() as connection:
            payment = connection.execute(
                """SELECT pp.*,u.email,u.full_name FROM payroll_payments pp
                   JOIN users u ON u.id=pp.user_id
                   WHERE pp.id=%s FOR UPDATE OF pp""",
                (payment_id,),
            ).fetchone()
            if not payment:
                raise ValueError("Payroll payment not found")
            if payment["status"] not in {"draft", "failed"}:
                raise ValueError("Only draft or failed payroll can be dispatched")
            existing_provider = str(payment["provider"] or "manual")
            existing_recipient = str(payment.get("recipient") or "")
            existing_currency = str(payment.get("recipient_currency") or "")
            if existing_provider not in {"manual", provider}:
                raise ValueError(
                    "A failed payroll can only be retried with its original provider"
                )
            if (
                provider == "paypal"
                and payment.get("delivery_started_at")
                and datetime.fromisoformat(payment["delivery_started_at"])
                < datetime.now(UTC) - timedelta(days=29)
            ):
                raise ValueError(
                    "PayPal's duplicate-protection window expired; reconcile this "
                    "payment manually instead of retrying"
                )
            recipient = existing_recipient
            recipient_currency = existing_currency
            if not recipient:
                if provider in {"paypal", "wise"}:
                    destination = connection.execute(
                        """SELECT recipient,currency FROM payroll_destinations
                           WHERE user_id=%s AND provider=%s
                           AND confirmed_at IS NOT NULL""",
                        (payment["user_id"], provider),
                    ).fetchone()
                    if not destination:
                        raise ValueError(
                            f"Confirm this member's {provider.title()} payout destination before sending"
                        )
                    recipient = destination["recipient"]
                    recipient_currency = destination["currency"]
                else:
                    recipient = payment["email"]
            result = connection.execute(
                """UPDATE payroll_payments SET status='processing',provider=%s,
                       recipient=%s,recipient_currency=%s,failure_reason='',updated_at=%s,
                       delivery_started_at=COALESCE(delivery_started_at,%s)
                   WHERE id=%s RETURNING *""",
                (
                    provider,
                    recipient,
                    recipient_currency,
                    utc_now(),
                    utc_now(),
                    payment_id,
                ),
            ).fetchone()
        claimed = dict(result)
        claimed["email"] = payment["email"]
        claimed["full_name"] = payment["full_name"]
        return claimed

    def reconcilable_payroll_payments(
        self, provider: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        if provider not in {"paypal", "wise"}:
            return []
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT pp.*,u.email,u.full_name FROM payroll_payments pp
                   JOIN users u ON u.id=pp.user_id
                   WHERE pp.provider=%s AND pp.status IN ('processing','paid','failed')
                     AND pp.external_reference <> ''
                     AND pp.next_reconcile_at <= CURRENT_TIMESTAMP
                     AND (
                         pp.status IN ('processing','failed')
                         OR pp.reconcile_until > CURRENT_TIMESTAMP
                     )
                   ORDER BY pp.next_reconcile_at,pp.id
                   LIMIT %s""",
                (provider, max(1, min(limit, 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    def defer_payroll_reconciliation(self, payment_id: str, reason: str) -> None:
        """Persist bounded exponential retry state after a provider outage."""

        now_datetime = datetime.now(UTC)
        with self.connect() as connection:
            current = connection.execute(
                """SELECT status,reconcile_attempts FROM payroll_payments
                   WHERE id=%s FOR UPDATE""",
                (payment_id,),
            ).fetchone()
            if not current:
                raise ValueError("Payroll payment not found")
            if current["status"] not in {"processing", "paid"}:
                return
            attempts = min(int(current["reconcile_attempts"] or 0) + 1, 20)
            base_minutes = 5 if current["status"] == "processing" else 60
            delay_minutes = min(base_minutes * (2 ** (attempts - 1)), 24 * 60)
            connection.execute(
                """UPDATE payroll_payments
                   SET reconcile_attempts=%s,last_reconcile_error=%s,
                       next_reconcile_at=%s,updated_at=%s WHERE id=%s""",
                (
                    attempts,
                    reason[:500],
                    (now_datetime + timedelta(minutes=delay_minutes)).isoformat(),
                    now_datetime.isoformat(),
                    payment_id,
                ),
            )

    def recover_stale_payroll_claims(self, provider: str, *, minutes: int = 10) -> int:
        """Make interrupted pre-reference dispatches safely retryable."""

        if provider not in {"webhook", "paypal", "wise"}:
            return 0
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE payroll_payments
                   SET status='failed',failure_reason='payroll_dispatch_interrupted',
                       updated_at=%s
                   WHERE provider=%s AND status='processing'
                     AND external_reference=''
                     AND updated_at < CURRENT_TIMESTAMP - (%s * INTERVAL '1 minute')""",
                (utc_now(), provider, max(1, min(minutes, 60))),
            )
        return max(0, result.rowcount)

    def record_payroll_delivery(
        self,
        payment_id: str,
        status: str,
        *,
        provider: str = "webhook",
        external_reference: str = "",
        failure_reason: str = "",
    ) -> None:
        if status not in {"processing", "paid", "failed", "reversed"}:
            raise ValueError("Invalid payroll delivery status")
        now_datetime = datetime.now(UTC)
        now = now_datetime.isoformat()
        paid_at = now if status == "paid" else None
        reversed_at = now if status == "reversed" else None
        next_reconcile_at = None
        reconcile_until = None
        if status == "processing" and external_reference:
            next_reconcile_at = (now_datetime + timedelta(minutes=5)).isoformat()
        elif status == "paid":
            next_reconcile_at = (now_datetime + timedelta(hours=24)).isoformat()
            reconcile_until = (now_datetime + timedelta(days=90)).isoformat()
        with self.connect() as connection:
            current = connection.execute(
                """SELECT status,provider,external_reference,paid_at,reconcile_until
                   FROM payroll_payments WHERE id=%s FOR UPDATE""",
                (payment_id,),
            ).fetchone()
            if not current:
                raise ValueError("Payroll payment not found")
            if current["status"] == "paid" and status not in {
                "processing",
                "paid",
                "reversed",
            }:
                raise ValueError(
                    "Paid payroll can only be reconciled as processing, paid, or reversed"
                )
            if current["status"] == "reversed" and status != "reversed":
                raise ValueError("Reversed payroll is terminal")
            current_provider = str(current["provider"] or "manual")
            if current_provider not in {"manual", provider}:
                raise ValueError("Payroll provider cannot be changed")
            current_reference = str(current["external_reference"] or "")
            if (
                current_reference
                and external_reference
                and current_reference != external_reference
            ):
                raise ValueError("Payroll provider reference cannot be changed")
            external_reference = external_reference or current_reference
            if status == "paid" and current["paid_at"]:
                paid_at = current["paid_at"]
                reconcile_until = current["reconcile_until"] or (
                    datetime.fromisoformat(current["paid_at"]) + timedelta(days=90)
                )
            result = connection.execute(
                """UPDATE payroll_payments
                   SET status=%s,provider=%s,external_reference=%s,failure_reason=%s,
                       paid_at=COALESCE(paid_at,%s),
                       reversed_at=COALESCE(reversed_at,%s),
                       next_reconcile_at=%s,reconcile_until=%s,
                       reconcile_attempts=0,last_reconcile_error='',updated_at=%s
                   WHERE id=%s""",
                (
                    status,
                    provider[:80],
                    external_reference[:255],
                    failure_reason[:1000],
                    paid_at,
                    reversed_at,
                    next_reconcile_at,
                    reconcile_until,
                    now,
                    payment_id,
                ),
            )
            if result.rowcount != 1:
                raise ValueError("Payroll payment not found")
            if status == "reversed" and current["status"] != "reversed":
                connection.execute(
                    """INSERT INTO audit_events(
                           id,actor_user_id,action,target_type,target_id,
                           occurred_at,details
                       ) VALUES (%s,NULL,%s,'payroll',%s,%s,%s)""",
                    (
                        str(uuid.uuid4()),
                        "payroll.provider_reversed",
                        payment_id,
                        now,
                        f"{provider}:{failure_reason}"[:500],
                    ),
                )

    def update_project_finances(
        self,
        project_id: str,
        color: str,
        budget_type: str,
        budget_amount: Decimal,
        budget_minutes: int,
        billable_rate: Decimal,
        client_id: str | None,
    ) -> None:
        if budget_type not in {"none", "hours", "cost"}:
            raise ValueError("Invalid budget type")
        with self.connect() as connection:
            connection.execute(
                """UPDATE projects SET color=%s,budget_type=%s,budget_amount=%s,budget_minutes=%s,billable_rate=%s,client_id=%s WHERE id=%s""",
                (
                    color[:20],
                    budget_type,
                    max(Decimal(0), budget_amount),
                    max(0, budget_minutes),
                    max(Decimal(0), billable_rate),
                    client_id or None,
                    project_id,
                ),
            )

    def web_timer_device(self, user_id: str, project_id: str | None) -> dict[str, Any]:
        """Return a stable virtual device used by the authenticated web timer."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM devices WHERE owner_user_id=%s AND platform='Web timer' LIMIT 1",
                (user_id,),
            ).fetchone()
            if not row:
                device_id = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO devices(id,name,token_hash,enabled,platform,created_at,
                                             owner_user_id,project_id,tracker_kind)
                       VALUES (%s,'Web timer',%s,TRUE,'Web timer',%s,%s,%s,'web')""",
                    (
                        device_id,
                        token_hash(str(uuid.uuid4())),
                        utc_now(),
                        user_id,
                        project_id,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM devices WHERE id=%s", (device_id,)
                ).fetchone()
        return dict(row)

    def active_timer(self, user_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT ws.*,p.name project_name,t.name task_name,
                          COALESCE(SUM(EXTRACT(EPOCH FROM (COALESCE(seg.ended_at,CURRENT_TIMESTAMP)-seg.started_at))),0)::BIGINT tracked_seconds
                   FROM work_sessions ws JOIN devices d ON d.id=ws.device_id
                   LEFT JOIN projects p ON p.id=ws.project_id LEFT JOIN tasks t ON t.id=ws.task_id
                   LEFT JOIN work_session_segments seg ON seg.session_id=ws.id
                   WHERE ws.user_id=%s AND d.platform='Web timer' AND ws.status IN ('active','paused')
                   GROUP BY ws.id,p.id,t.id ORDER BY ws.started_at DESC LIMIT 1""",
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def create_team(self, name: str, lead_user_id: str | None) -> str:
        team_id = str(uuid.uuid4())
        try:
            with self.connect() as connection:
                connection.execute(
                    "INSERT INTO teams(id,name,lead_user_id,created_at) VALUES (%s,%s,%s,%s)",
                    (team_id, name.strip()[:120], lead_user_id or None, utc_now()),
                )
                if lead_user_id:
                    connection.execute(
                        "INSERT INTO team_members(team_id,user_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                        (team_id, lead_user_id),
                    )
                    connection.execute(
                        "INSERT INTO team_leads(team_id,user_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                        (team_id, lead_user_id),
                    )
        except UniqueViolation as exc:
            raise ValueError("A team with that name already exists") from exc
        return team_id

    @staticmethod
    def _replace_scim_group_members(
        connection, team_id: str, member_ids: list[str]
    ) -> None:
        unique_ids = list(dict.fromkeys(member_ids))
        if unique_ids:
            rows = connection.execute(
                """SELECT id::text FROM users
                    WHERE id::text=ANY(%s) AND role='member' AND enabled=TRUE""",
                (unique_ids,),
            ).fetchall()
            if {row["id"] for row in rows} != set(unique_ids):
                raise ValueError(
                    "Every group member must be an active provisioned user"
                )
        connection.execute("DELETE FROM team_leads WHERE team_id=%s", (team_id,))
        connection.execute("DELETE FROM team_members WHERE team_id=%s", (team_id,))
        if unique_ids:
            connection.execute(
                """INSERT INTO team_members(team_id,user_id)
                    SELECT %s,value::uuid FROM unnest(%s::text[]) ids(value)""",
                (team_id, unique_ids),
            )

    def create_scim_group(
        self, name: str, external_id: str, member_ids: list[str]
    ) -> dict[str, Any]:
        team_id = str(uuid.uuid4())
        try:
            with self.connect() as connection:
                connection.execute(
                    """INSERT INTO teams(
                           id,name,created_at,scim_managed,scim_external_id,scim_updated_at
                       ) VALUES (%s,%s,%s,TRUE,%s,%s)""",
                    (
                        team_id,
                        name.strip()[:120],
                        utc_now(),
                        external_id[:512],
                        utc_now(),
                    ),
                )
                self._replace_scim_group_members(connection, team_id, member_ids)
        except UniqueViolation as exc:
            raise ValueError(
                "A group with that name or externalId already exists"
            ) from exc
        return self.get_scim_group(team_id)

    def get_scim_group(self, team_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT t.*,
                          COALESCE((SELECT jsonb_agg(jsonb_build_object(
                            'value',u.id::text,'display',COALESCE(NULLIF(u.full_name,''),u.email)
                          ) ORDER BY lower(u.email)) FROM team_members tm
                          JOIN users u ON u.id=tm.user_id
                          WHERE tm.team_id=t.id AND u.enabled=TRUE),'[]'::jsonb) members
                   FROM teams t WHERE t.id=%s AND t.scim_managed=TRUE""",
                (team_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_scim_groups(
        self, display_name: str | None, start_index: int, count: int
    ) -> tuple[list[dict[str, Any]], int]:
        where = "WHERE t.scim_managed=TRUE"
        parameters: list[Any] = []
        if display_name is not None:
            where += " AND lower(t.name)=lower(%s)"
            parameters.append(display_name)
        with self.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) count FROM teams t {where}", parameters
            ).fetchone()["count"]
            rows = connection.execute(
                f"""SELECT t.*,
                           COALESCE((SELECT jsonb_agg(jsonb_build_object(
                             'value',u.id::text,'display',COALESCE(NULLIF(u.full_name,''),u.email)
                           ) ORDER BY lower(u.email)) FROM team_members tm
                           JOIN users u ON u.id=tm.user_id
                           WHERE tm.team_id=t.id AND u.enabled=TRUE),'[]'::jsonb) members
                      FROM teams t {where}
                     ORDER BY lower(t.name) LIMIT %s OFFSET %s""",
                (*parameters, count, start_index - 1),
            ).fetchall()
        return [dict(row) for row in rows], int(total)

    def update_scim_group(
        self,
        team_id: str,
        *,
        name: str | None = None,
        external_id: str | None = None,
        member_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        fields = ["scim_updated_at=CURRENT_TIMESTAMP"]
        values: list[Any] = []
        if name is not None:
            fields.append("name=%s")
            values.append(name.strip()[:120])
        if external_id is not None:
            fields.append("scim_external_id=%s")
            values.append(external_id[:512])
        try:
            with self.connect() as connection:
                result = connection.execute(
                    f"UPDATE teams SET {','.join(fields)} WHERE id=%s AND scim_managed=TRUE",
                    (*values, team_id),
                )
                if result.rowcount != 1:
                    raise LookupError("SCIM group not found")
                if member_ids is not None:
                    self._replace_scim_group_members(connection, team_id, member_ids)
        except UniqueViolation as exc:
            raise ValueError(
                "A group with that name or externalId already exists"
            ) from exc
        return self.get_scim_group(team_id)

    def delete_scim_group(self, team_id: str) -> None:
        with self.connect() as connection:
            result = connection.execute(
                "DELETE FROM teams WHERE id=%s AND scim_managed=TRUE", (team_id,)
            )
            if result.rowcount != 1:
                raise LookupError("SCIM group not found")

    def team_lead_members(
        self, lead_user_id: str, permission: str
    ) -> list[dict[str, Any]]:
        column = self.TEAM_LEAD_PERMISSIONS.get(permission)
        if not column:
            raise ValueError("Unknown team-lead permission")
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT DISTINCT u.* FROM team_leads tl
                    JOIN team_members tm ON tm.team_id=tl.team_id
                    JOIN users u ON u.id=tm.user_id
                    WHERE tl.user_id=%s AND tl.{column}=TRUE
                      AND tm.user_id<>tl.user_id AND u.enabled=TRUE AND u.role='member'
                    ORDER BY u.email""",
                (lead_user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def team_lead_can_manage_user(
        self, lead_user_id: str, target_user_id: str, permission: str
    ) -> bool:
        if lead_user_id == target_user_id:
            return False
        column = self.TEAM_LEAD_PERMISSIONS.get(permission)
        if not column:
            raise ValueError("Unknown team-lead permission")
        with self.connect() as connection:
            row = connection.execute(
                f"""SELECT EXISTS(
                    SELECT 1 FROM team_leads tl
                    JOIN team_members tm ON tm.team_id=tl.team_id
                    JOIN users u ON u.id=tm.user_id
                    WHERE tl.user_id=%s AND tm.user_id=%s AND tl.{column}=TRUE
                      AND u.enabled=TRUE AND u.role='member'
                ) allowed""",
                (lead_user_id, target_user_id),
            ).fetchone()
        return bool(row["allowed"])

    def team_lead_can_manage_project(
        self, lead_user_id: str, project_id: str, permission: str
    ) -> bool:
        column = self.TEAM_LEAD_PERMISSIONS.get(permission)
        if column not in {"can_manage_projects", "can_manage_members"}:
            raise ValueError("Invalid team project permission")
        with self.connect() as connection:
            row = connection.execute(
                f"""SELECT EXISTS(
                    SELECT 1 FROM team_leads tl
                    JOIN team_projects tp ON tp.team_id=tl.team_id
                    WHERE tl.user_id=%s AND tp.project_id=%s AND tl.{column}=TRUE
                ) allowed""",
                (lead_user_id, project_id),
            ).fetchone()
        return bool(row["allowed"])

    def team_lead_can_schedule_user_project(
        self, lead_user_id: str, target_user_id: str, project_id: str
    ) -> bool:
        if lead_user_id == target_user_id:
            return False
        with self.connect() as connection:
            row = connection.execute(
                """SELECT EXISTS(
                       SELECT 1 FROM team_leads tl
                       JOIN team_members tm ON tm.team_id=tl.team_id
                       JOIN team_projects tp ON tp.team_id=tl.team_id
                       JOIN users u ON u.id=tm.user_id
                       WHERE tl.user_id=%s AND tm.user_id=%s
                         AND tp.project_id=%s AND tl.can_manage_schedules=TRUE
                         AND u.enabled=TRUE AND u.role='member'
                   ) allowed""",
                (lead_user_id, target_user_id, project_id),
            ).fetchone()
        return bool(row["allowed"])

    def team_lead_teams(
        self, lead_user_id: str, permission: str
    ) -> list[dict[str, Any]]:
        column = self.TEAM_LEAD_PERMISSIONS.get(permission)
        if not column:
            raise ValueError("Unknown team-lead permission")
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT t.id,t.name FROM team_leads tl
                    JOIN teams t ON t.id=tl.team_id
                    WHERE tl.user_id=%s AND tl.{column}=TRUE
                    ORDER BY lower(t.name)""",
                (lead_user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def team_lead_can_manage_team(
        self, lead_user_id: str, team_id: str, permission: str
    ) -> bool:
        column = self.TEAM_LEAD_PERMISSIONS.get(permission)
        if not column:
            raise ValueError("Unknown team-lead permission")
        with self.connect() as connection:
            row = connection.execute(
                f"""SELECT EXISTS(SELECT 1 FROM team_leads
                    WHERE user_id=%s AND team_id=%s AND {column}=TRUE) allowed""",
                (lead_user_id, team_id),
            ).fetchone()
        return bool(row["allowed"])

    def add_team_project(self, team_id: str, project_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO team_projects(team_id,project_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (team_id, project_id),
            )

    def remove_team_project(self, team_id: str, project_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM team_projects WHERE team_id=%s AND project_id=%s",
                (team_id, project_id),
            )

    def set_team_lead_permissions(
        self, team_id: str, user_id: str, permissions: dict[str, bool]
    ) -> None:
        values = {
            column: bool(permissions.get(name, False))
            for name, column in self.TEAM_LEAD_PERMISSIONS.items()
        }
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO team_members(team_id,user_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (team_id, user_id),
            )
            result = connection.execute(
                """INSERT INTO team_leads(
                    team_id,user_id,can_approve_timesheets,can_approve_manual_time,
                    can_approve_time_off,can_manage_schedules,can_manage_projects,
                    can_manage_members,can_manage_financials
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(team_id,user_id) DO UPDATE SET
                    can_approve_timesheets=EXCLUDED.can_approve_timesheets,
                    can_approve_manual_time=EXCLUDED.can_approve_manual_time,
                    can_approve_time_off=EXCLUDED.can_approve_time_off,
                    can_manage_schedules=EXCLUDED.can_manage_schedules,
                    can_manage_projects=EXCLUDED.can_manage_projects,
                    can_manage_members=EXCLUDED.can_manage_members,
                    can_manage_financials=EXCLUDED.can_manage_financials""",
                (
                    team_id,
                    user_id,
                    values["can_approve_timesheets"],
                    values["can_approve_manual_time"],
                    values["can_approve_time_off"],
                    values["can_manage_schedules"],
                    values["can_manage_projects"],
                    values["can_manage_members"],
                    values["can_manage_financials"],
                ),
            )
            if result.rowcount != 1:
                raise ValueError("Unable to update team lead")

    def add_team_member(self, team_id: str, user_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO team_members(team_id,user_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (team_id, user_id),
            )

    def remove_team_member(self, team_id: str, user_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM team_leads WHERE team_id=%s AND user_id=%s",
                (team_id, user_id),
            )
            connection.execute(
                "DELETE FROM team_members WHERE team_id=%s AND user_id=%s",
                (team_id, user_id),
            )

    def get_team(self, team_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM teams WHERE id=%s", (team_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_teams(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT t.*,u.email lead_email,u.full_name lead_name,COUNT(tm.user_id) member_count,
                          COALESCE(jsonb_agg(jsonb_build_object('id',mu.id,'email',mu.email,'full_name',mu.full_name))
                          FILTER (WHERE mu.id IS NOT NULL),'[]'::jsonb) members,
                          COALESCE((SELECT jsonb_agg(jsonb_build_object(
                            'id',lu.id,'email',lu.email,'full_name',lu.full_name,
                            'approve_timesheets',tl.can_approve_timesheets,
                            'approve_manual_time',tl.can_approve_manual_time,
                            'approve_time_off',tl.can_approve_time_off,
                            'manage_schedules',tl.can_manage_schedules,
                            'manage_projects',tl.can_manage_projects,
                            'manage_members',tl.can_manage_members,
                            'manage_financials',tl.can_manage_financials
                          ) ORDER BY lower(lu.email)) FROM team_leads tl
                          JOIN users lu ON lu.id=tl.user_id WHERE tl.team_id=t.id),'[]'::jsonb) leads,
                          COALESCE((SELECT jsonb_agg(jsonb_build_object(
                            'id',p.id,'name',p.name
                          ) ORDER BY lower(p.name)) FROM team_projects tp
                          JOIN projects p ON p.id=tp.project_id WHERE tp.team_id=t.id),'[]'::jsonb) projects
                   FROM teams t LEFT JOIN users u ON u.id=t.lead_user_id
                   LEFT JOIN team_members tm ON tm.team_id=t.id
                   LEFT JOIN users mu ON mu.id=tm.user_id
                   GROUP BY t.id,u.id ORDER BY lower(t.name)"""
            ).fetchall()
        return [dict(row) for row in rows]

    def create_geofence(
        self,
        name: str,
        project_id: str | None,
        latitude: float,
        longitude: float,
        radius_meters: int,
        enter_action: str = "none",
        exit_action: str = "none",
    ) -> str:
        if enter_action not in {"none", "start"} or exit_action not in {
            "none",
            "stop",
        }:
            raise ValueError("Invalid automatic timer action")
        geofence_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO geofences(id,name,project_id,latitude,longitude,radius_meters,enter_action,exit_action,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    geofence_id,
                    name.strip()[:120],
                    project_id or None,
                    latitude,
                    longitude,
                    radius_meters,
                    enter_action,
                    exit_action,
                    utc_now(),
                ),
            )
        return geofence_id

    def list_geofences(self, user_id: str | None = None) -> list[dict[str, Any]]:
        where = ""
        params: tuple[Any, ...] = ()
        if user_id:
            where = "WHERE g.project_id IS NULL OR EXISTS (SELECT 1 FROM project_members pm WHERE pm.project_id=g.project_id AND pm.user_id=%s)"
            params = (user_id,)
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT g.*,p.name project_name FROM geofences g LEFT JOIN projects p ON p.id=g.project_id
                   """
                + where
                + " ORDER BY g.active DESC,lower(g.name)",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def add_location(self, values: dict[str, Any]) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """INSERT INTO location_events(id,device_id,user_id,session_id,recorded_at,latitude,longitude,accuracy_meters,geofence_id,event_type,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(device_id,recorded_at) DO NOTHING""",
                (
                    values["id"],
                    values["device_id"],
                    values.get("user_id"),
                    values.get("session_id"),
                    values["recorded_at"],
                    values["latitude"],
                    values["longitude"],
                    values["accuracy_meters"],
                    values.get("geofence_id"),
                    values["event_type"],
                    utc_now(),
                ),
            )
        return result.rowcount == 1

    def geofence_state(
        self, user_id: str, latitude: float, longitude: float
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Return the containing fence and the member's previous fence.

        PostgreSQL's earthdistance extension isn't assumed, so use the standard
        haversine formula directly in SQL.
        """
        with self.connect() as connection:
            containing = connection.execute(
                """SELECT g.* FROM geofences g WHERE g.active=TRUE
                   AND (g.project_id IS NULL OR EXISTS (
                     SELECT 1 FROM project_members pm WHERE pm.project_id=g.project_id AND pm.user_id=%s
                   )) AND
                   6371000 * 2 * asin(sqrt(
                     power(sin(radians(%s-g.latitude)/2),2) +
                     cos(radians(g.latitude))*cos(radians(%s))*
                     power(sin(radians(%s-g.longitude)/2),2)
                   )) <= g.radius_meters
                   ORDER BY g.radius_meters LIMIT 1""",
                (user_id, latitude, latitude, longitude),
            ).fetchone()
            previous = connection.execute(
                """SELECT g.* FROM (
                     SELECT geofence_id,event_type FROM location_events
                     WHERE user_id=%s ORDER BY recorded_at DESC LIMIT 1
                   ) le JOIN geofences g ON g.id=le.geofence_id
                   WHERE le.event_type <> 'exit'""",
                (user_id,),
            ).fetchone()
        return (
            dict(containing) if containing else None,
            dict(previous) if previous else None,
        )

    def list_locations(self, user_id: str | None = None) -> list[dict[str, Any]]:
        where, params = ("WHERE le.user_id=%s", (user_id,)) if user_id else ("", ())
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT le.*,u.email,u.full_name,d.name device_name FROM location_events le
                    LEFT JOIN users u ON u.id=le.user_id JOIN devices d ON d.id=le.device_id
                    {where} ORDER BY le.recorded_at DESC LIMIT 500""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def attendance_report(
        self,
        user_id: str | None = None,
        *,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if (started_at is None) != (ended_at is None):
            raise ValueError("Both attendance report bounds are required")
        if started_at is not None and ended_at is not None and started_at >= ended_at:
            raise ValueError("Attendance report bounds are invalid")
        if limit is not None and not 1 <= limit <= 100_001:
            raise ValueError("Attendance report limit is invalid")
        conditions: list[str] = []
        parameters: list[Any] = []
        if user_id:
            conditions.append("s.user_id=%s")
            parameters.append(user_id)
        if started_at is not None:
            conditions.extend(["s.starts_at < %s", "s.ends_at > %s"])
            parameters.extend([ended_at, started_at])
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        limit_sql = "LIMIT %s" if limit is not None else ""
        if limit is not None:
            parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT s.id,u.email,u.full_name,s.starts_at,s.ends_at,p.name project_name,
                    COALESCE(SUM(EXTRACT(EPOCH FROM (LEAST(COALESCE(seg.ended_at,CURRENT_TIMESTAMP),s.ends_at)-GREATEST(seg.started_at,s.starts_at)))) FILTER (WHERE seg.started_at<s.ends_at AND COALESCE(seg.ended_at,CURRENT_TIMESTAMP)>s.starts_at),0)::BIGINT worked_seconds
                    FROM shifts s JOIN users u ON u.id=s.user_id LEFT JOIN projects p ON p.id=s.project_id
                    LEFT JOIN work_sessions ws ON ws.user_id=s.user_id
                      AND ws.started_at<s.ends_at
                      AND COALESCE(ws.ended_at,CURRENT_TIMESTAMP)>s.starts_at
                    LEFT JOIN work_session_segments seg ON seg.session_id=ws.id
                      AND seg.started_at<s.ends_at
                      AND COALESCE(seg.ended_at,CURRENT_TIMESTAMP)>s.starts_at
                    {where} GROUP BY s.id,u.id,p.id ORDER BY s.starts_at DESC
                    {limit_sql}""",
                tuple(parameters),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_integration(
        self, provider: str, display_name: str, webhook_url: str, creator_id: str
    ) -> str:
        integration_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO integrations(id,provider,display_name,webhook_url,created_by_user_id,created_at,updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (
                    integration_id,
                    provider[:60],
                    display_name.strip()[:120],
                    webhook_url.strip()[:1000],
                    creator_id,
                    utc_now(),
                    utc_now(),
                ),
            )
        return integration_id

    def list_integrations(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM integrations ORDER BY enabled DESC,lower(display_name)"
            ).fetchall()
        return [dict(row) for row in rows]

    def set_integration_enabled(self, integration_id: str, enabled: bool) -> None:
        with self.connect() as connection:
            result = connection.execute(
                "UPDATE integrations SET enabled=%s,updated_at=%s WHERE id=%s",
                (enabled, utc_now(), integration_id),
            )
            if result.rowcount != 1:
                raise ValueError("Integration not found")

    def time_report(
        self,
        *,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if (started_at is None) != (ended_at is None):
            raise ValueError("Both time report bounds are required")
        if started_at is not None and ended_at is not None and started_at >= ended_at:
            raise ValueError("Time report bounds are invalid")
        if limit is not None and not 1 <= limit <= 100_001:
            raise ValueError("Time report limit is invalid")
        if started_at is not None:
            limit_sql = "LIMIT %s" if limit is not None else ""
            parameters: tuple[Any, ...] = (started_at, ended_at)
            if limit is not None:
                parameters += (limit,)
            with self.connect() as connection:
                rows = connection.execute(
                    f"""WITH bounds AS (
                           SELECT %s::timestamptz started_at,%s::timestamptz ended_at
                       ), report_rows AS (
                           SELECT u.email,p.name project,t.name task,
                                  GREATEST(MIN(seg.started_at),b.started_at) started_at,
                                  MAX(LEAST(COALESCE(seg.ended_at,CURRENT_TIMESTAMP),b.ended_at)) ended_at,
                                  ws.status,
                                  COALESCE(SUM(EXTRACT(EPOCH FROM (
                                      LEAST(COALESCE(seg.ended_at,CURRENT_TIMESTAMP),b.ended_at)
                                      - GREATEST(seg.started_at,b.started_at)
                                  ))),0)::BIGINT seconds,
                                  'tracked' time_type
                           FROM work_sessions ws
                           JOIN work_session_segments seg ON seg.session_id=ws.id
                           CROSS JOIN bounds b
                           LEFT JOIN users u ON u.id=ws.user_id
                           LEFT JOIN projects p ON p.id=ws.project_id
                           LEFT JOIN tasks t ON t.id=ws.task_id
                           WHERE seg.started_at < b.ended_at
                             AND COALESCE(seg.ended_at,CURRENT_TIMESTAMP) > b.started_at
                           GROUP BY ws.id,u.id,p.id,t.id,b.started_at,b.ended_at
                           UNION ALL
                           SELECT u.email,p.name,t.name,
                                  GREATEST(m.started_at,b.started_at),
                                  LEAST(m.ended_at,b.ended_at),m.status,
                                  EXTRACT(EPOCH FROM (
                                      LEAST(m.ended_at,b.ended_at)
                                      - GREATEST(m.started_at,b.started_at)
                                  ))::BIGINT,'manual'
                           FROM manual_time_entries m CROSS JOIN bounds b
                           JOIN users u ON u.id=m.user_id
                           JOIN projects p ON p.id=m.project_id
                           LEFT JOIN tasks t ON t.id=m.task_id
                           WHERE m.started_at < b.ended_at
                             AND m.ended_at > b.started_at
                       ) SELECT * FROM report_rows
                         WHERE seconds > 0
                         ORDER BY started_at DESC
                         {limit_sql}""",
                    parameters,
                ).fetchall()
            return [dict(row) for row in rows]
        limit_sql = "LIMIT %s" if limit is not None else ""
        parameters = (limit,) if limit is not None else ()
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT u.email,p.name project,t.name task,ws.started_at,
                          MAX(seg.ended_at) ended_at,ws.status,COALESCE(SUM(EXTRACT(EPOCH FROM (COALESCE(seg.ended_at,CURRENT_TIMESTAMP)-seg.started_at))),0)::BIGINT seconds,
                          'tracked' time_type
                   FROM work_sessions ws LEFT JOIN users u ON u.id=ws.user_id
                   LEFT JOIN projects p ON p.id=ws.project_id LEFT JOIN tasks t ON t.id=ws.task_id
                   LEFT JOIN work_session_segments seg ON seg.session_id=ws.id
                   GROUP BY ws.id,u.id,p.id,t.id
                   UNION ALL
                   SELECT u.email,p.name,t.name,m.started_at,m.ended_at,m.status,
                          EXTRACT(EPOCH FROM (m.ended_at-m.started_at))::BIGINT,'manual'
                   FROM manual_time_entries m JOIN users u ON u.id=m.user_id
                   JOIN projects p ON p.id=m.project_id LEFT JOIN tasks t ON t.id=m.task_id
                   ORDER BY started_at DESC
                   {limit_sql}""",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def custom_time_report(
        self,
        started_at: datetime,
        ended_at: datetime,
        *,
        user_id: str | None = None,
        project_id: str | None = None,
        scope_user_id: str | None = None,
        scope_mode: str = "all",
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        if scope_mode not in {
            "all",
            "personal",
            "project_member",
            "project_visibility",
        }:
            raise ValueError("Invalid report scope")
        conditions = ["e.seconds > 0"]
        parameters: list[Any] = [
            started_at,
            ended_at,
            ended_at,
            started_at,
            started_at,
            ended_at,
            ended_at,
            started_at,
            started_at,
            ended_at,
            ended_at,
            started_at,
            ended_at,
            started_at,
        ]
        if user_id:
            conditions.append("e.user_id=%s")
            parameters.append(user_id)
        if project_id:
            conditions.append("e.project_id=%s")
            parameters.append(project_id)
        if scope_mode == "personal":
            conditions.append("e.user_id=%s")
            parameters.append(scope_user_id)
        elif scope_mode == "project_member":
            conditions.append(
                "EXISTS (SELECT 1 FROM project_members pm "
                "WHERE pm.project_id=e.project_id AND pm.user_id=%s)"
            )
            parameters.append(scope_user_id)
        elif scope_mode == "project_visibility":
            conditions.append(
                "(e.user_id=%s OR EXISTS (SELECT 1 FROM project_members pm "
                "WHERE pm.project_id=e.project_id AND pm.user_id=%s "
                "AND pm.project_role IN ('manager','viewer')))"
            )
            parameters.extend((scope_user_id, scope_user_id))
        parameters.append(min(max(limit, 1), 10_001))
        where = " AND ".join(conditions)
        with self.connect() as connection:
            rows = connection.execute(
                f"""WITH entries AS (
                    SELECT ws.id entry_id,ws.user_id,ws.project_id,ws.task_id,
                           u.email,u.full_name,p.name project,t.name task,c.name client,
                           GREATEST(MIN(seg.started_at),%s) started_at,
                           LEAST(MAX(COALESCE(seg.ended_at,CURRENT_TIMESTAMP)),%s) ended_at,
                           SUM(EXTRACT(EPOCH FROM (
                               LEAST(COALESCE(seg.ended_at,CURRENT_TIMESTAMP),%s)
                               - GREATEST(seg.started_at,%s)
                           )))::BIGINT seconds,
                           'tracked' time_type,ws.status,
                           COALESCE(stats.activity_percent,0)::INTEGER activity_percent,
                           COALESCE(stats.keyboard_events,0)::BIGINT keyboard_events,
                           COALESCE(stats.mouse_clicks,0)::BIGINT mouse_clicks
                    FROM work_sessions ws
                    JOIN work_session_segments seg ON seg.session_id=ws.id
                    LEFT JOIN users u ON u.id=ws.user_id
                    LEFT JOIN projects p ON p.id=ws.project_id
                    LEFT JOIN tasks t ON t.id=ws.task_id
                    LEFT JOIN clients c ON c.id=p.client_id
                    LEFT JOIN LATERAL (
                        SELECT ROUND(AVG(a.activity_percent)) activity_percent,
                               SUM(a.keyboard_events) keyboard_events,
                               SUM(a.mouse_clicks) mouse_clicks
                        FROM activity_records a
                        WHERE a.session_id=ws.id AND a.captured_at >= %s
                          AND a.captured_at < %s
                    ) stats ON TRUE
                    WHERE seg.started_at < %s
                      AND COALESCE(seg.ended_at,CURRENT_TIMESTAMP) > %s
                    GROUP BY ws.id,u.id,p.id,t.id,c.id,stats.activity_percent,
                             stats.keyboard_events,stats.mouse_clicks
                    UNION ALL
                    SELECT m.id,m.user_id,m.project_id,m.task_id,
                           u.email,u.full_name,p.name,t.name,c.name,
                           GREATEST(m.started_at,%s),LEAST(m.ended_at,%s),
                           EXTRACT(EPOCH FROM (
                               LEAST(m.ended_at,%s)-GREATEST(m.started_at,%s)
                           ))::BIGINT,
                           'manual',m.status,0,0,0
                    FROM manual_time_entries m
                    JOIN users u ON u.id=m.user_id
                    JOIN projects p ON p.id=m.project_id
                    LEFT JOIN tasks t ON t.id=m.task_id
                    LEFT JOIN clients c ON c.id=p.client_id
                    WHERE m.started_at < %s AND m.ended_at > %s
                )
                SELECT * FROM entries e WHERE {where}
                ORDER BY e.started_at DESC,e.email,e.project LIMIT %s""",
                tuple(parameters),
            ).fetchall()
        return [dict(row) for row in rows]

    def quickbooks_export_configuration(self) -> dict[str, Any]:
        with self.connect() as connection:
            settings = connection.execute(
                """SELECT quickbooks_company_name,quickbooks_company_create_time,
                          quickbooks_default_service_item,quickbooks_timezone
                   FROM organization_settings WHERE id=1"""
            ).fetchone()
            users = connection.execute(
                """SELECT id,email,full_name,quickbooks_name FROM users
                   WHERE enabled=TRUE ORDER BY lower(COALESCE(NULLIF(full_name,''),email))"""
            ).fetchall()
            projects = connection.execute(
                """SELECT id,name,
                          CASE WHEN enabled THEN 'active' ELSE 'archived' END status,
                          quickbooks_customer_job,quickbooks_class,quickbooks_billable
                   FROM projects ORDER BY enabled DESC,lower(name)"""
            ).fetchall()
            tasks = connection.execute(
                """SELECT t.id,t.project_id,t.name,t.status,t.billable,
                          t.quickbooks_service_item,p.name project_name
                   FROM tasks t JOIN projects p ON p.id=t.project_id
                   ORDER BY p.enabled DESC,lower(p.name),t.status,lower(t.name)"""
            ).fetchall()
        return {
            "settings": dict(settings),
            "users": [dict(row) for row in users],
            "projects": [dict(row) for row in projects],
            "tasks": [dict(row) for row in tasks],
        }

    def update_quickbooks_export_settings(
        self,
        company_name: str,
        company_create_time: str,
        default_service_item: str,
        timezone_name: str = "UTC",
    ) -> None:
        company = validate_mapping(
            company_name, "QuickBooks company name", max_length=255
        )
        service_item = validate_mapping(
            default_service_item, "Default QuickBooks service item"
        )
        company_time = company_create_time.strip()
        if company_time and (
            not company_time.isascii()
            or not company_time.isdigit()
            or len(company_time) > 20
        ):
            raise QuickBooksIIFError(
                "QuickBooks company creation time must be the numeric value exported by QuickBooks"
            )
        try:
            ZoneInfo(timezone_name)
        except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
            raise QuickBooksIIFError("Enter a valid IANA QuickBooks timezone") from exc
        with self.connect() as connection:
            connection.execute(
                """UPDATE organization_settings
                   SET quickbooks_company_name=%s,quickbooks_company_create_time=%s,
                       quickbooks_default_service_item=%s,quickbooks_timezone=%s,
                       updated_at=%s
                   WHERE id=1""",
                (company, company_time, service_item, timezone_name, utc_now()),
            )

    def update_quickbooks_user_mapping(self, user_id: str, name: str) -> None:
        value = validate_mapping(name, "QuickBooks employee name")
        with self.connect() as connection:
            result = connection.execute(
                "UPDATE users SET quickbooks_name=%s WHERE id=%s",
                (value, user_id),
            )
            if result.rowcount != 1:
                raise ValueError("User not found")

    def update_quickbooks_project_mapping(
        self,
        project_id: str,
        customer_job: str,
        class_name: str,
        billable: bool,
    ) -> None:
        customer = validate_mapping(customer_job, "QuickBooks customer/job")
        quickbooks_class = validate_mapping(
            class_name, "QuickBooks class", max_length=159
        )
        if billable and not customer:
            raise QuickBooksIIFError(
                "Billable general project time requires a QuickBooks customer/job"
            )
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE projects SET quickbooks_customer_job=%s,
                          quickbooks_class=%s,quickbooks_billable=%s
                   WHERE id=%s""",
                (customer, quickbooks_class, billable, project_id),
            )
            if result.rowcount != 1:
                raise ValueError("Project not found")

    def update_quickbooks_task_mapping(self, task_id: str, service_item: str) -> None:
        value = validate_mapping(service_item, "QuickBooks service item")
        with self.connect() as connection:
            result = connection.execute(
                "UPDATE tasks SET quickbooks_service_item=%s WHERE id=%s",
                (value, task_id),
            )
            if result.rowcount != 1:
                raise ValueError("Task not found")

    def quickbooks_time_rows(
        self,
        started_at: datetime,
        ended_at: datetime,
        *,
        approved_only: bool = True,
        user_id: str | None = None,
        project_id: str | None = None,
        timezone_name: str = "UTC",
        limit: int = 10_001,
    ) -> list[dict[str, Any]]:
        if started_at >= ended_at:
            raise ValueError("QuickBooks report bounds are invalid")
        if not 1 <= limit <= 10_001:
            raise ValueError("QuickBooks report limit is invalid")
        try:
            ZoneInfo(timezone_name)
        except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("Invalid QuickBooks report timezone") from exc
        with self.connect() as connection:
            rows = connection.execute(
                """WITH bounds AS (
                       SELECT %s::timestamptz started_at,%s::timestamptz ended_at,
                              %s::text timezone_name
                   ), entries AS (
                       SELECT ws.user_id,ws.project_id,ws.task_id,ws.note,
                              seg.started_at,seg.ended_at,
                              u.quickbooks_name,p.quickbooks_customer_job,
                              p.quickbooks_class,
                              CASE WHEN t.id IS NULL THEN p.quickbooks_billable
                                   ELSE t.billable END quickbooks_billable,
                              t.quickbooks_service_item
                       FROM work_sessions ws
                       JOIN work_session_segments seg ON seg.session_id=ws.id
                       JOIN users u ON u.id=ws.user_id
                       JOIN projects p ON p.id=ws.project_id
                       LEFT JOIN tasks t ON t.id=ws.task_id
                       CROSS JOIN bounds b
                       WHERE ws.status='stopped' AND seg.ended_at IS NOT NULL
                         AND seg.started_at < b.ended_at
                         AND seg.ended_at > b.started_at
                       UNION ALL
                       SELECT m.user_id,m.project_id,m.task_id,m.note,
                              m.started_at,m.ended_at,
                              u.quickbooks_name,p.quickbooks_customer_job,
                              p.quickbooks_class,
                              CASE WHEN t.id IS NULL THEN p.quickbooks_billable
                                   ELSE t.billable END,
                              t.quickbooks_service_item
                       FROM manual_time_entries m
                       JOIN users u ON u.id=m.user_id
                       JOIN projects p ON p.id=m.project_id
                       LEFT JOIN tasks t ON t.id=m.task_id
                       CROSS JOIN bounds b
                       WHERE m.status='approved' AND m.started_at < b.ended_at
                         AND m.ended_at > b.started_at
                   ), scoped AS (
                       SELECT e.* FROM entries e
                       WHERE (%s::uuid IS NULL OR e.user_id=%s::uuid)
                         AND (%s::uuid IS NULL OR e.project_id=%s::uuid)
                   ), pieces AS (
                       SELECT e.*,day.work_date,
                              EXTRACT(EPOCH FROM (
                                  LEAST(
                                      e.ended_at,
                                      (day.work_date + INTERVAL '1 day')
                                          AT TIME ZONE b.timezone_name,
                                      b.ended_at
                                  ) - GREATEST(
                                      e.started_at,
                                      day.work_date AT TIME ZONE b.timezone_name,
                                      b.started_at
                                  )
                              ))::BIGINT seconds
                       FROM scoped e CROSS JOIN bounds b
                       CROSS JOIN LATERAL generate_series(
                           date_trunc(
                               'day',GREATEST(e.started_at,b.started_at)
                                     AT TIME ZONE b.timezone_name
                           ),
                           date_trunc(
                               'day',(LEAST(e.ended_at,b.ended_at)
                                      - INTERVAL '1 microsecond')
                                     AT TIME ZONE b.timezone_name
                           ),
                           INTERVAL '1 day'
                       ) day(work_date)
                   )
                   SELECT user_id,project_id,task_id,work_date::date work_date,note,
                          quickbooks_name,quickbooks_customer_job,quickbooks_class,
                          quickbooks_billable,quickbooks_service_item,
                          SUM(seconds)::BIGINT seconds
                   FROM pieces
                   WHERE seconds > 0 AND (
                       %s=FALSE OR EXISTS (
                           SELECT 1 FROM timesheets sheet
                           WHERE sheet.user_id=pieces.user_id
                             AND sheet.status='approved'
                             AND pieces.work_date::date BETWEEN
                                 sheet.period_start AND sheet.period_end
                       )
                   )
                   GROUP BY user_id,project_id,task_id,work_date::date,note,
                            quickbooks_name,quickbooks_customer_job,quickbooks_class,
                            quickbooks_billable,quickbooks_service_item
                   ORDER BY work_date,user_id,project_id,task_id NULLS FIRST,note
                   LIMIT %s""",
                (
                    started_at,
                    ended_at,
                    timezone_name,
                    user_id,
                    user_id,
                    project_id,
                    project_id,
                    approved_only,
                    limit,
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_saved_report_filter(
        self,
        owner_user_id: str,
        name: str,
        description: str,
        configuration: dict[str, Any],
    ) -> str:
        filter_id = str(uuid.uuid4())
        now = utc_now()
        try:
            with self.connect() as connection:
                connection.execute(
                    """INSERT INTO saved_report_filters(
                           id,owner_user_id,name,description,configuration,created_at,updated_at
                       ) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        filter_id,
                        owner_user_id,
                        name.strip()[:120],
                        description.strip()[:500],
                        Jsonb(configuration),
                        now,
                        now,
                    ),
                )
        except UniqueViolation as exc:
            raise ValueError("A saved report with that name already exists") from exc
        return filter_id

    def list_saved_report_filters(self, owner_user_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM saved_report_filters WHERE owner_user_id=%s
                   ORDER BY updated_at DESC,lower(name)""",
                (owner_user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_saved_report_filter(
        self, filter_id: str, owner_user_id: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM saved_report_filters
                   WHERE id=%s AND owner_user_id=%s""",
                (filter_id, owner_user_id),
            ).fetchone()
        return dict(row) if row else None

    def delete_saved_report_filter(self, filter_id: str, owner_user_id: str) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                "DELETE FROM saved_report_filters WHERE id=%s AND owner_user_id=%s",
                (filter_id, owner_user_id),
            )
        return result.rowcount == 1

    def list_notifications(self, user_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM notifications WHERE user_id=%s OR user_id IS NULL
                   ORDER BY created_at DESC LIMIT 100""",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_notifications_read(self, user_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE notifications SET read_at=%s WHERE user_id=%s AND read_at IS NULL",
                (utc_now(), user_id),
            )

    def create_global_todo(
        self,
        name: str,
        description: str,
        project_ids: list[str],
        add_to_future_projects: bool,
        creator_id: str,
    ) -> str:
        todo_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO global_todos(id,name,description,add_to_future_projects,created_by_user_id,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (
                    todo_id,
                    name.strip()[:160],
                    description.strip()[:500],
                    add_to_future_projects,
                    creator_id,
                    utc_now(),
                ),
            )
            for project_id in project_ids:
                connection.execute(
                    "INSERT INTO project_todos(todo_id,project_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (todo_id, project_id),
                )
        return todo_id

    def list_project_todos(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT gt.*,pt.completed_at,pt.assigned_user_id,u.email assigned_email
                   FROM project_todos pt JOIN global_todos gt ON gt.id=pt.todo_id
                   LEFT JOIN users u ON u.id=pt.assigned_user_id WHERE pt.project_id=%s
                   ORDER BY pt.completed_at NULLS FIRST,gt.created_at DESC""",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_todo_complete(
        self, todo_id: str, project_id: str, user_id: str, complete: bool
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE project_todos SET completed_at=CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END,
                   completed_by_user_id=CASE WHEN %s THEN %s::uuid ELSE NULL END
                   WHERE todo_id=%s AND project_id=%s""",
                (complete, complete, user_id, todo_id, project_id),
            )

    def create_scheduled_report(
        self,
        name: str,
        report_type: str,
        frequency: str,
        recipients: str,
        creator_id: str,
        delivery_format: str = "csv",
        delivery_time: time = time(9, 0),
        schedule_weekday: int = 0,
        schedule_month_day: int = 1,
        range_preset: str = "previous_period",
    ) -> str:
        cleaned_name = name.strip()
        if not cleaned_name or any(character in cleaned_name for character in "\r\n"):
            raise ValueError("Enter a valid report name")
        if report_type not in {"time", "activity", "attendance", "expenses"}:
            raise ValueError("Invalid report type")
        if frequency not in {"daily", "weekly", "monthly"}:
            raise ValueError("Invalid report frequency")
        if delivery_format not in {"csv", "pdf"}:
            raise ValueError("Invalid report delivery format")
        if range_preset not in REPORT_RANGE_PRESETS:
            raise ValueError("Invalid report range")
        if (
            not isinstance(delivery_time, time)
            or delivery_time.tzinfo is not None
            or delivery_time.second
            or delivery_time.microsecond
        ):
            raise ValueError("Report delivery time must be an exact UTC minute")
        if not isinstance(schedule_weekday, int) or isinstance(schedule_weekday, bool):
            raise ValueError("Invalid report weekday")
        if not isinstance(schedule_month_day, int) or isinstance(
            schedule_month_day, bool
        ):
            raise ValueError("Invalid report month day")
        now = datetime.now(UTC)
        next_send_at = next_report_delivery(
            now,
            frequency,
            delivery_time,
            weekday=schedule_weekday,
            month_day=schedule_month_day,
        )
        recipient_list = [
            value.strip()
            for value in recipients.replace(";", ",").split(",")
            if value.strip()
        ]
        if not recipient_list or any(
            value.count("@") != 1
            or any(character in value for character in "\r\n")
            or len(value) > 320
            for value in recipient_list
        ):
            raise ValueError("Enter valid report recipient emails")
        report_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO scheduled_reports(
                       id,name,report_type,frequency,recipients,delivery_format,
                       delivery_hour,delivery_minute,schedule_weekday,
                       schedule_month_day,range_preset,next_send_at,
                       created_by_user_id,created_at
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    report_id,
                    cleaned_name[:120],
                    report_type[:60],
                    frequency,
                    recipients.strip()[:1000],
                    delivery_format,
                    delivery_time.hour,
                    delivery_time.minute,
                    schedule_weekday,
                    schedule_month_day,
                    range_preset,
                    next_send_at,
                    creator_id,
                    utc_now(),
                ),
            )
        return report_id

    def list_scheduled_reports(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM scheduled_reports ORDER BY enabled DESC,created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def set_scheduled_report_enabled(self, report_id: str, enabled: bool) -> None:
        with self.connect() as connection:
            report = connection.execute(
                "SELECT * FROM scheduled_reports WHERE id=%s FOR UPDATE", (report_id,)
            ).fetchone()
            if not report:
                raise ValueError("Scheduled report not found")
            next_send_at = report["next_send_at"]
            if enabled:
                next_send_at = next_report_delivery(
                    datetime.now(UTC),
                    report["frequency"],
                    time(report["delivery_hour"], report["delivery_minute"]),
                    weekday=report["schedule_weekday"],
                    month_day=report["schedule_month_day"],
                )
            connection.execute(
                """UPDATE scheduled_reports
                   SET enabled=%s,next_send_at=%s,
                       consecutive_failures=CASE WHEN %s THEN 0 ELSE consecutive_failures END,
                       last_error_code=CASE WHEN %s THEN '' ELSE last_error_code END,
                       delivery_claim_token=NULL,delivery_claim_until=NULL
                   WHERE id=%s""",
                (enabled, next_send_at, enabled, enabled, report_id),
            )

    def delete_scheduled_report(self, report_id: str) -> None:
        with self.connect() as connection:
            result = connection.execute(
                "DELETE FROM scheduled_reports WHERE id=%s", (report_id,)
            )
            if result.rowcount != 1:
                raise ValueError("Scheduled report not found")

    def due_scheduled_reports(
        self,
        now: datetime,
        claim_token: str,
        *,
        lease_seconds: int = 30 * 60,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        try:
            claim_token = str(uuid.UUID(claim_token))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Invalid scheduled report claim token") from exc
        if not 60 <= lease_seconds <= 2 * 60 * 60:
            raise ValueError("Invalid scheduled report claim lease")
        if not 1 <= limit <= 100:
            raise ValueError("Invalid scheduled report claim limit")
        with self.connect() as connection:
            rows = connection.execute(
                """WITH candidates AS (
                       SELECT id FROM scheduled_reports
                       WHERE enabled=TRUE
                         AND next_send_at IS NOT NULL AND next_send_at <= %s
                         AND (
                           delivery_claim_until IS NULL OR delivery_claim_until <= %s
                         )
                       ORDER BY next_send_at,id
                       FOR UPDATE SKIP LOCKED
                       LIMIT %s
                   )
                   UPDATE scheduled_reports report
                   SET delivery_claim_token=%s,
                       delivery_claim_until=%s + (%s * INTERVAL '1 second')
                   FROM candidates
                   WHERE report.id=candidates.id
                   RETURNING report.*""",
                (now, now, limit, claim_token, now, lease_seconds),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_scheduled_report_sent(
        self,
        report_id: str,
        sent_at: datetime,
        next_send_at: datetime,
        claim_token: str | None = None,
    ) -> None:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE scheduled_reports
                   SET last_sent_at=%s,next_send_at=%s,consecutive_failures=0,
                       last_error_code='',delivery_claim_token=NULL,
                       delivery_claim_until=NULL
                   WHERE id=%s
                     AND (%s::uuid IS NULL OR delivery_claim_token=%s::uuid)""",
                (sent_at, next_send_at, report_id, claim_token, claim_token),
            )
            if result.rowcount != 1:
                raise ValueError("Scheduled report delivery claim was lost")

    def mark_scheduled_report_failed(
        self,
        report_id: str,
        failed_at: datetime,
        next_attempt_at: datetime,
        error_code: str,
        claim_token: str | None = None,
    ) -> None:
        if not error_code or len(error_code) > 64:
            raise ValueError("Invalid scheduled report error code")
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE scheduled_reports
                   SET consecutive_failures=consecutive_failures+1,
                       last_failed_at=%s,last_error_code=%s,next_send_at=%s,
                       delivery_claim_token=NULL,delivery_claim_until=NULL
                   WHERE id=%s
                     AND (%s::uuid IS NULL OR delivery_claim_token=%s::uuid)""",
                (
                    failed_at,
                    error_code,
                    next_attempt_at,
                    report_id,
                    claim_token,
                    claim_token,
                ),
            )
            if result.rowcount != 1:
                raise ValueError("Scheduled report delivery claim was lost")

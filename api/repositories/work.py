from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from psycopg import Connection
from psycopg.errors import UniqueViolation

from .base import RepositoryMixin, utc_now


class WorkRepository(RepositoryMixin):
    def create_task(
        self,
        project_id: str,
        name: str,
        description: str,
        created_by_user_id: str,
        *,
        billable: bool = True,
    ) -> dict[str, Any]:
        task = {
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "name": name.strip(),
            "description": description.strip(),
            "billable": bool(billable),
            "created_at": utc_now(),
            "created_by_user_id": created_by_user_id,
        }
        try:
            with self.connect() as connection:
                connection.execute(
                    """INSERT INTO tasks(
                           id, project_id, name, description, billable,
                           created_at, created_by_user_id
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    tuple(task.values()),
                )
        except UniqueViolation as exc:
            raise ValueError("That task already exists in this project") from exc
        return task

    def list_tasks(
        self, project_id: str, *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        status_filter = "" if include_archived else "AND status = 'active'"
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM tasks WHERE project_id = %s {status_filter}
                    ORDER BY lower(name)""",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = %s", (task_id,)
            ).fetchone()
        return dict(row) if row else None

    def sync_work_session(
        self, device: dict[str, Any], status: str, task_id: str | None
    ) -> dict[str, Any] | None:
        if status not in {"active", "paused", "stopped"}:
            return None
        if (
            status == "active"
            and device.get("owner_user_id")
            and self.is_period_locked(device["owner_user_id"], datetime.now(UTC).date())
        ):
            raise ValueError("The current timesheet period is approved and locked")
        project_id = device.get("project_id")
        if task_id:
            task = self.get_task(task_id)
            if (
                not task
                or task["project_id"] != project_id
                or task["status"] != "active"
            ):
                raise ValueError("Task is not active in this device's project")
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "SELECT id FROM devices WHERE id = %s FOR UPDATE", (device["id"],)
            )
            session = connection.execute(
                """SELECT * FROM work_sessions
                   WHERE device_id = %s AND status IN ('active', 'paused')
                   ORDER BY started_at DESC LIMIT 1""",
                (device["id"],),
            ).fetchone()
            if session and session["task_id"] != task_id:
                self._stop_session(connection, session["id"], now)
                session = None
            if status == "stopped":
                if session:
                    self._stop_session(connection, session["id"], now)
                return None
            if not session:
                session_id = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO work_sessions(
                           id, user_id, device_id, project_id, task_id, status,
                           started_at, created_at, updated_at
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        session_id,
                        device.get("owner_user_id"),
                        device["id"],
                        project_id,
                        task_id,
                        status,
                        now,
                        now,
                        now,
                    ),
                )
                if status == "active":
                    self._start_segment(connection, session_id, now)
            else:
                session_id = session["id"]
                if status == "paused" and session["status"] == "active":
                    self._close_segment(connection, session_id, now)
                elif status == "active" and session["status"] == "paused":
                    self._start_segment(connection, session_id, now)
                connection.execute(
                    "UPDATE work_sessions SET status = %s, updated_at = %s WHERE id = %s",
                    (status, now, session_id),
                )
            row = connection.execute(
                "SELECT * FROM work_sessions WHERE id = %s", (session_id,)
            ).fetchone()
        return dict(row)

    def get_work_session(self, session_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM work_sessions WHERE id = %s", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_work_sessions(
        self, user_id: str | None = None, project_id: str | None = None
    ) -> list[dict[str, Any]]:
        filters: list[str] = []
        values: list[str] = []
        if user_id:
            filters.append("s.user_id = %s")
            values.append(user_id)
        if project_id:
            filters.append("s.project_id = %s")
            values.append(project_id)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT s.*, p.name AS project_name, t.name AS task_name,
                           COALESCE(SUM(GREATEST(
                               0, EXTRACT(EPOCH FROM (COALESCE(g.ended_at, CURRENT_TIMESTAMP) - g.started_at))
                           )), 0)::BIGINT AS tracked_seconds
                    FROM work_sessions s
                    LEFT JOIN projects p ON p.id = s.project_id
                    LEFT JOIN tasks t ON t.id = s.task_id
                    LEFT JOIN work_session_segments g ON g.session_id = s.id
                    {where}
                    GROUP BY s.id ORDER BY s.started_at DESC""",
                tuple(values),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _start_segment(
        connection: Connection, session_id: str, started_at: str
    ) -> None:
        connection.execute(
            """INSERT INTO work_session_segments(id, session_id, started_at)
               VALUES (%s, %s, %s)""",
            (str(uuid.uuid4()), session_id, started_at),
        )

    @staticmethod
    def _close_segment(connection: Connection, session_id: str, ended_at: str) -> None:
        connection.execute(
            """UPDATE work_session_segments SET ended_at = %s
               WHERE session_id = %s AND ended_at IS NULL""",
            (ended_at, session_id),
        )

    @classmethod
    def _stop_session(
        cls, connection: Connection, session_id: str, ended_at: str
    ) -> None:
        cls._close_segment(connection, session_id, ended_at)
        connection.execute(
            """UPDATE work_sessions
               SET status = 'stopped', ended_at = %s, updated_at = %s WHERE id = %s""",
            (ended_at, ended_at, session_id),
        )

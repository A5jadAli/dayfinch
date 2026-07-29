from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
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
        self,
        device: dict[str, Any],
        status: str,
        task_id: str | None,
        *,
        event_id: str | None = None,
        observed_at: datetime | None = None,
        idle_seconds: int = 0,
        heartbeat_interval_seconds: int = 60,
    ) -> dict[str, Any] | None:
        if status not in {"active", "paused", "stopped"}:
            return None
        if (
            status == "active"
            and device.get("owner_user_id")
            and self.is_period_locked(
                device["owner_user_id"], (observed_at or datetime.now(UTC)).date()
            )
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
        observed = observed_at or datetime.now(UTC)
        if observed.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        observed = observed.astimezone(UTC)
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "SELECT id FROM devices WHERE id = %s FOR UPDATE", (device["id"],)
            )
            if event_id:
                duplicate = connection.execute(
                    """SELECT session_id FROM agent_state_events
                       WHERE device_id = %s AND id = %s""",
                    (device["id"], event_id),
                ).fetchone()
                if duplicate:
                    return (
                        self._session_in_connection(connection, duplicate["session_id"])
                        if duplicate["session_id"]
                        else None
                    )

                previous = connection.execute(
                    """SELECT * FROM agent_state_events
                       WHERE device_id = %s ORDER BY observed_at DESC LIMIT 1""",
                    (device["id"],),
                ).fetchone()
                if previous:
                    previous_at = datetime.fromisoformat(previous["observed_at"])
                    if observed < previous_at:
                        # Wall clocks can step backwards after sleep or NTP repair.
                        # Preserve queue progress while never rewriting older time.
                        observed = previous_at

            session = connection.execute(
                """SELECT * FROM work_sessions
                   WHERE device_id = %s AND status IN ('active', 'paused')
                   ORDER BY started_at DESC LIMIT 1""",
                (device["id"],),
            ).fetchone()
            # A run of journalled heartbeats proves continuous offline work. A gap
            # means the process or computer was down, so cap the previous segment
            # at one heartbeat after its last durable observation.
            if event_id and previous and session and previous["status"] == "active":
                previous_at = datetime.fromisoformat(previous["observed_at"])
                lease = max(
                    90,
                    int(previous["heartbeat_interval_seconds"]) * 2,
                    heartbeat_interval_seconds * 2,
                )
                if (observed - previous_at).total_seconds() > lease:
                    self._stop_session(
                        connection,
                        session["id"],
                        previous_at
                        + timedelta(
                            seconds=int(previous["heartbeat_interval_seconds"])
                        ),
                    )
                    session = None
            if session and session["task_id"] != task_id:
                self._stop_session(connection, session["id"], observed)
                session = None
            if status == "stopped":
                if session:
                    self._stop_session(connection, session["id"], observed)
                if event_id:
                    self._insert_state_event(
                        connection,
                        event_id,
                        device["id"],
                        observed,
                        status,
                        task_id,
                        idle_seconds,
                        heartbeat_interval_seconds,
                        None,
                    )
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
                        observed,
                        now,
                        now,
                    ),
                )
                if status == "active":
                    self._start_segment(connection, session_id, observed)
            else:
                session_id = session["id"]
                if status == "paused" and session["status"] == "active":
                    ended_at = observed - timedelta(seconds=idle_seconds)
                    self._close_segment(connection, session_id, ended_at)
                elif status == "active" and session["status"] == "paused":
                    self._start_segment(connection, session_id, observed)
                connection.execute(
                    "UPDATE work_sessions SET status = %s, updated_at = %s WHERE id = %s",
                    (status, now, session_id),
                )
            if event_id:
                self._insert_state_event(
                    connection,
                    event_id,
                    device["id"],
                    observed,
                    status,
                    task_id,
                    idle_seconds,
                    heartbeat_interval_seconds,
                    session_id,
                )
            row = connection.execute(
                "SELECT * FROM work_sessions WHERE id = %s", (session_id,)
            ).fetchone()
        return dict(row)

    @staticmethod
    def _session_in_connection(
        connection: Connection, session_id: str
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM work_sessions WHERE id = %s", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _insert_state_event(
        connection: Connection,
        event_id: str,
        device_id: str,
        observed_at: datetime,
        status: str,
        task_id: str | None,
        idle_seconds: int,
        heartbeat_interval_seconds: int,
        session_id: str | None,
    ) -> None:
        connection.execute(
            """INSERT INTO agent_state_events(
                   id, device_id, observed_at, status, task_id, idle_seconds,
                   heartbeat_interval_seconds, session_id
               ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                event_id,
                device_id,
                observed_at,
                status,
                task_id,
                idle_seconds,
                heartbeat_interval_seconds,
                session_id,
            ),
        )

    def get_work_session(self, session_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM work_sessions WHERE id = %s", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_work_session_for_capture(
        self, device_id: str, captured_at: datetime
    ) -> dict[str, Any] | None:
        """Resolve a queued offline capture after its state journal is replayed."""
        with self.connect() as connection:
            row = connection.execute(
                """SELECT s.* FROM work_sessions s
                   JOIN work_session_segments g ON g.session_id = s.id
                   WHERE s.device_id = %s
                     AND g.started_at <= %s
                     AND (g.ended_at IS NULL OR g.ended_at >= %s)
                   ORDER BY g.started_at DESC LIMIT 1""",
                (device_id, captured_at, captured_at),
            ).fetchone()
        return dict(row) if row else None

    def delete_state_events_before(self, cutoff: str) -> int:
        """Compact replay receipts while preserving each device's latest lease."""
        with self.connect() as connection:
            result = connection.execute(
                """DELETE FROM agent_state_events old
                   WHERE old.received_at < %s
                     AND EXISTS (
                         SELECT 1 FROM agent_state_events newer
                         WHERE newer.device_id = old.device_id
                           AND newer.observed_at > old.observed_at
                     )""",
                (cutoff,),
            )
        return max(0, result.rowcount)

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
                    GROUP BY s.id, p.name, t.name ORDER BY s.started_at DESC""",
                tuple(values),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _start_segment(
        connection: Connection, session_id: str, started_at: str | datetime
    ) -> None:
        connection.execute(
            """INSERT INTO work_session_segments(id, session_id, started_at)
               VALUES (%s, %s, %s)""",
            (str(uuid.uuid4()), session_id, started_at),
        )

    @staticmethod
    def _close_segment(
        connection: Connection, session_id: str, ended_at: str | datetime
    ) -> None:
        connection.execute(
            """UPDATE work_session_segments SET ended_at = GREATEST(started_at, %s)
               WHERE session_id = %s AND ended_at IS NULL""",
            (ended_at, session_id),
        )

    @classmethod
    def _stop_session(
        cls, connection: Connection, session_id: str, ended_at: str | datetime
    ) -> None:
        cls._close_segment(connection, session_id, ended_at)
        connection.execute(
            """UPDATE work_sessions
               SET status = 'stopped', ended_at = %s, updated_at = %s WHERE id = %s""",
            (ended_at, ended_at, session_id),
        )

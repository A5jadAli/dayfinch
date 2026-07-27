from __future__ import annotations

from typing import Any

from psycopg.errors import UniqueViolation

from .base import RepositoryMixin, utc_now


class ActivityRepository(RepositoryMixin):
    def record_exists(self, record_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM activity_records WHERE id = %s", (record_id,)
            ).fetchone()
        return row is not None

    def add_record(self, record: dict[str, Any]) -> bool:
        try:
            with self.connect() as connection:
                connection.execute(
                    """INSERT INTO activity_records(
                           id, device_id, captured_at, received_at, keyboard_events,
                           mouse_clicks, mouse_distance, active_app, agent_version,
                           screenshot_path, storage_version_id, focused_seconds,
                           interactive_seconds, user_id, project_id, task_id, session_id
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        record["id"],
                        record["device_id"],
                        record["captured_at"],
                        utc_now(),
                        record["keyboard_events"],
                        record["mouse_clicks"],
                        record["mouse_distance"],
                        record.get("active_app"),
                        record["agent_version"],
                        record["screenshot_path"],
                        record.get("storage_version_id"),
                        record.get("focused_seconds", 0),
                        record.get("interactive_seconds", 0),
                        record.get("user_id"),
                        record.get("project_id"),
                        record.get("task_id"),
                        record.get("session_id"),
                    ),
                )
            return True
        except UniqueViolation:
            return False

    def list_records(self, device_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM activity_records WHERE device_id = %s
                   ORDER BY captured_at DESC LIMIT %s""",
                (device_id, min(max(limit, 1), 500)),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_record(self, record_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT a.*, d.owner_user_id FROM activity_records a
                   JOIN devices d ON d.id = a.device_id WHERE a.id = %s""",
                (record_id,),
            ).fetchone()
        return dict(row) if row else None

    def records_before(self, cutoff: str, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM activity_records WHERE captured_at < %s
                   ORDER BY captured_at ASC LIMIT %s""",
                (cutoff, min(max(limit, 1), 5_000)),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_record(self, record_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM activity_records WHERE id = %s", (record_id,))

    def activity_report(self, project_id: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE a.project_id = %s" if project_id else ""
        parameters: tuple[Any, ...] = (project_id,) if project_id else ()
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT p.name AS project, u.email AS member,
                           a.captured_at::date AS work_date,
                           COUNT(a.id) AS intervals,
                           SUM(a.focused_seconds) AS focused_seconds,
                           SUM(a.interactive_seconds) AS interactive_seconds,
                           SUM(a.keyboard_events) AS keyboard_events,
                           SUM(a.mouse_clicks) AS mouse_clicks
                    FROM activity_records a
                    JOIN devices d ON d.id = a.device_id
                    LEFT JOIN projects p ON p.id = a.project_id
                    LEFT JOIN users u ON u.id = a.user_id
                    {where}
                    GROUP BY p.id, u.id, work_date
                    ORDER BY work_date DESC, p.name, u.email""",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from .base import RepositoryMixin, utc_now


class ProjectsRepository(RepositoryMixin):
    def create_project(
        self, name: str, description: str, created_by_user_id: str
    ) -> dict[str, Any]:
        project = {
            "id": str(uuid.uuid4()),
            "name": name.strip(),
            "description": description.strip(),
            "created_at": utc_now(),
            "created_by_user_id": created_by_user_id,
        }
        try:
            with self.connect() as connection:
                connection.execute(
                    """INSERT INTO projects(id, name, description, created_at, created_by_user_id)
                       VALUES (?, ?, ?, ?, ?)""",
                    tuple(project.values()),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("A project with that name already exists") from exc
        return project

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT p.*,
                          (SELECT COUNT(*) FROM project_members pm WHERE pm.project_id = p.id) member_count,
                          (SELECT COUNT(*) FROM devices d WHERE d.project_id = p.id) device_count
                   FROM projects p WHERE p.id = ?""",
                (project_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_projects(self, user_id: str | None = None) -> list[dict[str, Any]]:
        where = (
            ""
            if user_id is None
            else "WHERE EXISTS (SELECT 1 FROM project_members pm WHERE pm.project_id = p.id AND pm.user_id = ?)"
        )
        parameters: tuple[Any, ...] = () if user_id is None else (user_id,)
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT p.*,
                           (SELECT COUNT(*) FROM project_members pm WHERE pm.project_id = p.id) member_count,
                           (SELECT COUNT(*) FROM devices d WHERE d.project_id = p.id) device_count,
                           (SELECT COUNT(*) FROM activity_records a JOIN devices d ON d.id = a.device_id WHERE d.project_id = p.id) record_count
                    FROM projects p {where} ORDER BY p.name COLLATE NOCASE""",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def add_project_member(self, project_id: str, user_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO project_members(project_id, user_id, added_at) VALUES (?, ?, ?)",
                (project_id, user_id, utc_now()),
            )

    def is_project_member(self, project_id: str, user_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM project_members WHERE project_id = ? AND user_id = ?",
                (project_id, user_id),
            ).fetchone()
        return row is not None

    def list_project_members(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT u.id, u.email, u.role, u.enabled, pm.added_at
                   FROM project_members pm JOIN users u ON u.id = pm.user_id
                   WHERE pm.project_id = ? ORDER BY u.email COLLATE NOCASE""",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

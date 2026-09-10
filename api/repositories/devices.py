from __future__ import annotations

import secrets
import uuid
from typing import Any

from .base import RepositoryMixin, token_hash, utc_now


class DevicesRepository(RepositoryMixin):
    def create_device(
        self,
        name: str,
        owner_user_id: str | None = None,
        project_id: str | None = None,
        tracker_kind: str = "desktop",
    ) -> tuple[dict[str, Any], str]:
        if tracker_kind not in {"desktop", "mobile"}:
            raise ValueError("Tracker kind must be desktop or mobile")
        raw_token = secrets.token_urlsafe(32)
        device = {
            "id": str(uuid.uuid4()),
            "name": name.strip(),
            "created_at": utc_now(),
            "owner_user_id": owner_user_id,
            "project_id": project_id,
            "tracker_kind": tracker_kind,
        }
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO devices(id,name,token_hash,created_at,owner_user_id,
                                         project_id,tracker_kind)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (
                    device["id"],
                    device["name"],
                    token_hash(raw_token),
                    device["created_at"],
                    owner_user_id,
                    project_id,
                    tracker_kind,
                ),
            )
        return device, raw_token

    def authenticate_device(self, raw_token: str) -> dict[str, Any] | None:
        if not raw_token:
            return None
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM devices WHERE token_hash = %s AND enabled = TRUE",
                (token_hash(raw_token),),
            ).fetchone()
        return dict(row) if row else None

    def touch_device(self, device_id: str, platform: str, status: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE devices SET last_seen_at = %s,
                          platform = COALESCE(NULLIF(%s, ''), platform), last_status = %s
                   WHERE id = %s""",
                (utc_now(), platform[:80], status[:40], device_id),
            )

    def list_devices(
        self,
        owner_user_id: str | None = None,
        project_id: str | None = None,
        project_member_id: str | None = None,
    ) -> list[dict[str, Any]]:
        filters: list[str] = []
        values: list[str] = []
        if owner_user_id is not None:
            filters.append("d.owner_user_id = %s")
            values.append(owner_user_id)
        if project_id is not None:
            filters.append("d.project_id = %s")
            values.append(project_id)
        if project_member_id is not None:
            filters.append(
                "EXISTS (SELECT 1 FROM project_members pm "
                "WHERE pm.project_id=d.project_id AND pm.user_id=%s)"
            )
            values.append(project_member_id)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT d.*, u.email AS owner_email, p.name AS project_name,
                           COUNT(a.id) AS record_count, MAX(a.captured_at) AS latest_capture
                    FROM devices d
                    LEFT JOIN users u ON u.id = d.owner_user_id
                    LEFT JOIN projects p ON p.id = d.project_id
                    LEFT JOIN activity_records a ON a.device_id = d.id
                    {where}
                    GROUP BY d.id, u.email, p.name
                    ORDER BY d.created_at DESC""",
                tuple(values),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT d.*, u.email AS owner_email, p.name AS project_name FROM devices d
                   LEFT JOIN users u ON u.id = d.owner_user_id
                   LEFT JOIN projects p ON p.id = d.project_id WHERE d.id = %s""",
                (device_id,),
            ).fetchone()
        return dict(row) if row else None

    def set_device_enabled(self, device_id: str, enabled: bool) -> None:
        with self.connect() as connection:
            device = connection.execute(
                "SELECT id FROM devices WHERE id=%s FOR UPDATE", (device_id,)
            ).fetchone()
            if not device:
                return
            connection.execute(
                "UPDATE devices SET enabled = %s WHERE id = %s",
                (enabled, device_id),
            )
            if not enabled:
                observed_at = utc_now()
                connection.execute(
                    """UPDATE work_session_segments seg
                       SET ended_at=GREATEST(%s,seg.started_at)
                       WHERE seg.ended_at IS NULL AND EXISTS (
                         SELECT 1 FROM work_sessions ws
                         WHERE ws.id=seg.session_id AND ws.device_id=%s
                       )""",
                    (observed_at, device_id),
                )
                connection.execute(
                    """UPDATE work_breaks b
                       SET ended_at=GREATEST(%s,b.started_at)
                       WHERE b.ended_at IS NULL AND EXISTS (
                         SELECT 1 FROM work_sessions ws
                         WHERE ws.id=b.session_id AND ws.device_id=%s
                       )""",
                    (observed_at, device_id),
                )
                connection.execute(
                    """UPDATE work_sessions SET
                         status='stopped',ended_at=GREATEST(%s,started_at),
                         updated_at=%s
                       WHERE device_id=%s AND status IN ('active','paused')""",
                    (observed_at, observed_at, device_id),
                )

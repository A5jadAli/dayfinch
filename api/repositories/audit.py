from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from .base import RepositoryMixin, utc_now


class AuditRepository(RepositoryMixin):
    def add_audit_event(
        self,
        actor_user_id: str | None,
        action: str,
        target_type: str,
        target_id: str | None = None,
        details: str = "",
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO audit_events(
                       id, actor_user_id, action, target_type, target_id, occurred_at, details
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    str(uuid.uuid4()),
                    actor_user_id,
                    action[:80],
                    target_type[:40],
                    target_id,
                    utc_now(),
                    details[:500],
                ),
            )

    def audit_report(
        self,
        started_at: datetime,
        ended_at: datetime,
        *,
        actor_user_id: str | None = None,
        system_actor: bool = False,
        action: str = "",
        target_type: str = "",
        target_user_id: str | None = None,
        limit: int = 101,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 10_001 or not 0 <= offset <= 10_000:
            raise ValueError("Invalid audit report page")
        conditions = ["event.occurred_at>=%s", "event.occurred_at<%s"]
        parameters: list[Any] = [started_at, ended_at]
        if system_actor:
            conditions.append("event.actor_user_id IS NULL")
        elif actor_user_id:
            conditions.append("event.actor_user_id=%s")
            parameters.append(actor_user_id)
        if action:
            conditions.append("event.action=%s")
            parameters.append(action)
        if target_type:
            conditions.append("event.target_type=%s")
            parameters.append(target_type)
        if target_user_id:
            conditions.extend(["event.target_type='user'", "event.target_id=%s"])
            parameters.append(target_user_id)
        parameters.extend([limit, offset])
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT event.id,event.occurred_at,event.action,
                           event.target_type,event.details,event.actor_user_id,
                           event.target_id,
                           COALESCE(NULLIF(actor.full_name,''),actor.email,'System')
                             author,
                           COALESCE(NULLIF(subject.full_name,''),subject.email,'')
                             affected_member
                    FROM audit_events event
                    LEFT JOIN users actor ON actor.id=event.actor_user_id
                    LEFT JOIN users subject
                      ON event.target_type='user' AND subject.id=event.target_id
                    WHERE {" AND ".join(conditions)}
                    ORDER BY event.occurred_at DESC,event.id DESC
                    LIMIT %s OFFSET %s""",
                tuple(parameters),
            ).fetchall()
        return [dict(row) for row in rows]

    def audit_report_filter_values(
        self, started_at: datetime, ended_at: datetime
    ) -> dict[str, list[str]]:
        with self.connect() as connection:
            actions = connection.execute(
                """SELECT DISTINCT action FROM audit_events
                   WHERE occurred_at>=%s AND occurred_at<%s
                   ORDER BY action LIMIT 500""",
                (started_at, ended_at),
            ).fetchall()
            target_types = connection.execute(
                """SELECT DISTINCT target_type FROM audit_events
                   WHERE occurred_at>=%s AND occurred_at<%s
                   ORDER BY target_type LIMIT 100""",
                (started_at, ended_at),
            ).fetchall()
        return {
            "actions": [str(row["action"]) for row in actions],
            "target_types": [str(row["target_type"]) for row in target_types],
        }

    def purge_audit_events(self, cutoff: datetime, *, limit: int = 10_000) -> int:
        if not 1 <= limit <= 10_000:
            raise ValueError("Invalid audit retention batch")
        with self.connect() as connection:
            result = connection.execute(
                """DELETE FROM audit_events WHERE id IN (
                       SELECT id FROM audit_events WHERE occurred_at<%s
                       ORDER BY occurred_at,id LIMIT %s
                   )""",
                (cutoff, limit),
            )
        return result.rowcount

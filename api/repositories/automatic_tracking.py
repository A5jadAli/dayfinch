from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.errors import UniqueViolation

from .base import RepositoryMixin, utc_now


class AutomaticTrackingRepository(RepositoryMixin):
    def create_automatic_tracking_policy(
        self,
        name: str,
        rule_type: str,
        project_id: str,
        wait_for_activity: bool,
        schedule: dict[str, list[dict[str, str]]],
        user_ids: list[str],
        creator_id: str,
    ) -> str:
        if rule_type not in {"fixed_schedule", "shifts"}:
            raise ValueError("Invalid automatic tracking rule")
        clean_name = name.strip()[:120]
        if not clean_name:
            raise ValueError("Policy name is required")
        assignments = list(dict.fromkeys(user_ids))
        if not assignments:
            raise ValueError("Assign at least one member")
        if rule_type == "fixed_schedule" and not schedule:
            raise ValueError("A fixed schedule needs at least one working day")
        policy_id = str(uuid.uuid4())
        now = utc_now()
        try:
            with self.connect() as connection:
                project = connection.execute(
                    "SELECT enabled FROM projects WHERE id=%s", (project_id,)
                ).fetchone()
                if not project or not project["enabled"]:
                    raise ValueError("Starting project is unavailable")
                eligible = {
                    row["id"]
                    for row in connection.execute(
                        """SELECT u.id FROM users u
                           LEFT JOIN project_members pm
                             ON pm.user_id=u.id AND pm.project_id=%s
                           WHERE u.id=ANY(%s::uuid[]) AND u.enabled=TRUE
                             AND (u.role IN ('admin','manager')
                                  OR pm.project_role IN ('worker','manager'))""",
                        (project_id, assignments),
                    ).fetchall()
                }
                if eligible != set(assignments):
                    raise ValueError("One or more assigned members are unavailable")
                connection.execute(
                    "DELETE FROM automatic_tracking_assignments WHERE user_id=ANY(%s::uuid[])",
                    (assignments,),
                )
                connection.execute(
                    """INSERT INTO automatic_tracking_policies(
                           id,name,rule_type,project_id,wait_for_activity,schedule,
                           created_by_user_id,created_at,updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)""",
                    (
                        policy_id,
                        clean_name,
                        rule_type,
                        project_id,
                        wait_for_activity,
                        json.dumps(schedule),
                        creator_id,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """INSERT INTO automatic_tracking_assignments(
                           policy_id,user_id,assigned_at)
                       SELECT %s,assigned_user_id,%s
                       FROM unnest(%s::uuid[]) AS assigned_user_id""",
                    (policy_id, now, assignments),
                )
        except UniqueViolation as exc:
            raise ValueError(
                "An automatic tracking policy already uses that name"
            ) from exc
        return policy_id

    def list_automatic_tracking_policies(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT p.*,pr.name project_name,
                          COALESCE(jsonb_agg(jsonb_build_object(
                              'user_id',u.id,'email',u.email,'full_name',u.full_name,
                              'consent_status',a.consent_status)
                              ORDER BY lower(u.email)) FILTER (WHERE u.id IS NOT NULL),'[]') assignments
                   FROM automatic_tracking_policies p
                   JOIN projects pr ON pr.id=p.project_id
                   LEFT JOIN automatic_tracking_assignments a ON a.policy_id=p.id
                   LEFT JOIN users u ON u.id=a.user_id
                   GROUP BY p.id,pr.name ORDER BY p.enabled DESC,lower(p.name)"""
            ).fetchall()
        return [dict(row) for row in rows]

    def get_automatic_tracking_policy(self, policy_id: str) -> dict[str, Any] | None:
        return next(
            (
                policy
                for policy in self.list_automatic_tracking_policies()
                if policy["id"] == policy_id
            ),
            None,
        )

    def update_automatic_tracking_policy(
        self,
        policy_id: str,
        name: str,
        rule_type: str,
        project_id: str,
        wait_for_activity: bool,
        schedule: dict[str, list[dict[str, str]]],
        user_ids: list[str],
    ) -> bool:
        if rule_type not in {"fixed_schedule", "shifts"}:
            raise ValueError("Invalid automatic tracking rule")
        clean_name = name.strip()[:120]
        assignments = list(dict.fromkeys(user_ids))
        if not clean_name:
            raise ValueError("Policy name is required")
        if not assignments:
            raise ValueError("Assign at least one member")
        if rule_type == "fixed_schedule" and not schedule:
            raise ValueError("A fixed schedule needs at least one working day")
        now = utc_now()
        try:
            with self.connect() as connection:
                existing = connection.execute(
                    "SELECT id FROM automatic_tracking_policies WHERE id=%s FOR UPDATE",
                    (policy_id,),
                ).fetchone()
                if not existing:
                    return False
                project = connection.execute(
                    "SELECT enabled FROM projects WHERE id=%s", (project_id,)
                ).fetchone()
                if not project or not project["enabled"]:
                    raise ValueError("Starting project is unavailable")
                eligible = {
                    row["id"]
                    for row in connection.execute(
                        """SELECT u.id FROM users u
                           LEFT JOIN project_members pm
                             ON pm.user_id=u.id AND pm.project_id=%s
                           WHERE u.id=ANY(%s::uuid[]) AND u.enabled=TRUE
                             AND (u.role IN ('admin','manager')
                                  OR pm.project_role IN ('worker','manager'))""",
                        (project_id, assignments),
                    ).fetchall()
                }
                if eligible != set(assignments):
                    raise ValueError("One or more assigned members are unavailable")
                connection.execute(
                    """UPDATE automatic_tracking_policies SET
                           name=%s,rule_type=%s,project_id=%s,wait_for_activity=%s,
                           schedule=%s::jsonb,updated_at=%s WHERE id=%s""",
                    (
                        clean_name,
                        rule_type,
                        project_id,
                        wait_for_activity,
                        json.dumps(schedule),
                        now,
                        policy_id,
                    ),
                )
                connection.execute(
                    "DELETE FROM automatic_tracking_assignments WHERE policy_id=%s",
                    (policy_id,),
                )
                connection.execute(
                    "DELETE FROM automatic_tracking_assignments WHERE user_id=ANY(%s::uuid[])",
                    (assignments,),
                )
                connection.execute(
                    """INSERT INTO automatic_tracking_assignments(
                           policy_id,user_id,assigned_at)
                       SELECT %s,assigned_user_id,%s
                       FROM unnest(%s::uuid[]) AS assigned_user_id""",
                    (policy_id, now, assignments),
                )
        except UniqueViolation as exc:
            raise ValueError(
                "An automatic tracking policy already uses that name"
            ) from exc
        return True

    def delete_automatic_tracking_policy(self, policy_id: str) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                "DELETE FROM automatic_tracking_policies WHERE id=%s", (policy_id,)
            )
        return result.rowcount == 1

    def automatic_tracking_for_user(
        self, user_id: str, *, now: datetime | None = None
    ) -> dict[str, Any] | None:
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        with self.connect() as connection:
            row = connection.execute(
                """SELECT p.*,a.consent_status,a.responded_at
                   FROM automatic_tracking_assignments a
                   JOIN automatic_tracking_policies p ON p.id=a.policy_id
                   JOIN projects pr ON pr.id=p.project_id
                   WHERE a.user_id=%s AND p.enabled=TRUE AND pr.enabled=TRUE""",
                (user_id,),
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            result["shift_windows"] = []
            if row["rule_type"] == "shifts":
                result["shift_windows"] = [
                    dict(shift)
                    for shift in connection.execute(
                        """SELECT starts_at,ends_at,COALESCE(project_id,%s) project_id
                           FROM shifts WHERE user_id=%s AND published=TRUE
                             AND ends_at >= %s AND starts_at <= %s
                           ORDER BY starts_at LIMIT 100""",
                        (
                            row["project_id"],
                            user_id,
                            moment - timedelta(days=1),
                            moment + timedelta(days=14),
                        ),
                    ).fetchall()
                ]
        return result

    def respond_to_automatic_tracking_policy(
        self, user_id: str, policy_id: str, accepted: bool
    ) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE automatic_tracking_assignments
                   SET consent_status=%s,responded_at=%s
                   WHERE user_id=%s AND policy_id=%s""",
                ("accepted" if accepted else "declined", utc_now(), user_id, policy_id),
            )
        return result.rowcount == 1

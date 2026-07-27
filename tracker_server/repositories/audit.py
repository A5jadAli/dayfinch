from __future__ import annotations

import uuid

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
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
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

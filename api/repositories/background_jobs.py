from __future__ import annotations

from .base import RepositoryMixin


class BackgroundJobsRepository(RepositoryMixin):
    def monitoring_backlogs(self) -> dict[str, int]:
        """Return only aggregate durable-queue depths for Prometheus."""
        with self.connect() as connection:
            row = connection.execute(
                """SELECT
                     (SELECT COUNT(*) FROM jira_worklog_dirty_days)
                       AS jira_worklog_dirty,
                     (SELECT COUNT(*) FROM jira_worklog_exports
                       WHERE synced_at IS NULL
                          OR synced_seconds IS DISTINCT FROM desired_seconds
                          OR synced_started_at IS DISTINCT FROM desired_started_at)
                       AS jira_worklog_outbox,
                     (SELECT COUNT(*) FROM asana_comment_dirty_days)
                       AS asana_comment_dirty,
                     (SELECT COUNT(*) FROM asana_comment_exports
                       WHERE synced_at IS NULL
                          OR synced_seconds IS DISTINCT FROM desired_seconds
                          OR synced_started_at IS DISTINCT FROM desired_started_at)
                       AS asana_comment_outbox,
                     (SELECT COUNT(*) FROM slack_outbox
                       WHERE sent_at IS NULL AND discarded_at IS NULL)
                       AS slack_outbox"""
            ).fetchone()
        return {name: int(value) for name, value in row.items()}

    def claim_background_job(
        self, name: str, owner_id: str, lease_seconds: int
    ) -> bool:
        if not name.strip() or not owner_id.strip() or lease_seconds < 1:
            raise ValueError("A job name, owner, and positive lease are required")
        with self.connect() as connection:
            claimed = connection.execute(
                """
                INSERT INTO background_job_leases(
                    name, owner_id, lease_until, updated_at
                ) VALUES (
                    %s, %s, CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT(name) DO UPDATE SET
                    owner_id = EXCLUDED.owner_id,
                    lease_until = EXCLUDED.lease_until,
                    updated_at = CURRENT_TIMESTAMP
                WHERE background_job_leases.lease_until <= CURRENT_TIMESTAMP
                   OR background_job_leases.owner_id = EXCLUDED.owner_id
                RETURNING name
                """,
                (name, owner_id, lease_seconds),
            ).fetchone()
        return claimed is not None

    def release_background_jobs(self, owner_id: str) -> int:
        with self.connect() as connection:
            result = connection.execute(
                "DELETE FROM background_job_leases WHERE owner_id = %s",
                (owner_id,),
            )
        return result.rowcount

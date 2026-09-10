from __future__ import annotations

from .base import RepositoryMixin


class BackgroundJobsRepository(RepositoryMixin):
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

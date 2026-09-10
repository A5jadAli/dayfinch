from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..database import Database
from ..storage import ScreenshotStore


class RetentionService:
    def __init__(
        self,
        database: Database,
        storage: ScreenshotStore,
        retention_days: int,
        audit_retention_days: int = 2555,
    ) -> None:
        self.database = database
        self.storage = storage
        self.retention_days = retention_days
        self.audit_retention_days = audit_retention_days

    def purge_expired(self) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=self.retention_days)).isoformat()
        deleted = 0
        try:
            while records := self.database.records_before(cutoff):
                for record in records:
                    self.storage.delete(
                        record["screenshot_path"], record.get("storage_version_id")
                    )
                    self.database.delete_record(record["id"])
                    deleted += 1
        finally:
            # Independent telemetry still expires when object storage is down. A
            # failed S3 deletion keeps its screenshot metadata for a safe retry,
            # but must not extend app/domain/GPS retention indefinitely.
            self.database.delete_state_events_before(cutoff)
            self.database.delete_context_events_before(cutoff)
            audit_cutoff = datetime.now(UTC) - timedelta(days=self.audit_retention_days)
            audit_deleted = self.database.purge_audit_events(audit_cutoff)
            if audit_deleted:
                self.database.add_audit_event(
                    None,
                    "retention.audit_purged",
                    "audit",
                    None,
                    f"rows={audit_deleted};retention_days={self.audit_retention_days}",
                )
        return deleted

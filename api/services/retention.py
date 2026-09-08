from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from ..database import Database
from ..storage import ScreenshotStore


class RetentionService:
    def __init__(
        self, database: Database, storage: ScreenshotStore, retention_days: int
    ) -> None:
        self.database = database
        self.storage = storage
        self.retention_days = retention_days

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
        return deleted


async def run_retention_worker(
    service: RetentionService, interval_seconds: int = 60 * 60
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        await asyncio.to_thread(service.purge_expired)

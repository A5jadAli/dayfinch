from datetime import datetime, timedelta, timezone

import pytest

from api.database import Database
from api.services.retention import RetentionService


class FakeStorage:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.deleted: list[tuple[str, str | None]] = []

    def delete(self, key: str, version_id: str | None = None) -> None:
        if self.fail:
            raise OSError("storage unavailable")
        self.deleted.append((key, version_id))


def _old_record(database: Database) -> str:
    device, _ = database.create_device("Laptop")
    record_id = "9f84cd50-ee80-47de-9886-b33d49a5ecb2"
    database.add_record(
        {
            "id": record_id,
            "device_id": device["id"],
            "captured_at": (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(),
            "keyboard_events": 0,
            "mouse_clicks": 0,
            "mouse_distance": 0,
            "active_app": "Editor",
            "agent_version": "0.2.0",
            "screenshot_path": "old.jpg",
            "storage_version_id": "version-1",
        }
    )
    return record_id


def test_retention_deletes_storage_before_metadata(database: Database):
    record_id = _old_record(database)
    storage = FakeStorage()

    deleted = RetentionService(database, storage, 30).purge_expired()

    assert deleted == 1
    assert storage.deleted == [("old.jpg", "version-1")]
    assert database.get_record(record_id) is None


def test_retention_preserves_metadata_when_storage_fails(database: Database):
    record_id = _old_record(database)

    with pytest.raises(OSError, match="storage unavailable"):
        RetentionService(database, FakeStorage(fail=True), 30).purge_expired()

    assert database.get_record(record_id) is not None

from datetime import UTC, datetime, timedelta
from uuid import uuid4

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
            "captured_at": (datetime.now(UTC) - timedelta(days=60)).isoformat(),
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
    record = database.get_record(record_id)
    usage_id = str(uuid4())
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO usage_records(
                   id,device_id,observed_at,active_app,focused_seconds
               ) VALUES (%s,%s,%s,'Browser',10)""",
            (
                usage_id,
                record["device_id"],
                datetime.now(UTC) - timedelta(days=60),
            ),
        )

    with pytest.raises(OSError, match="storage unavailable"):
        RetentionService(database, FakeStorage(fail=True), 30).purge_expired()

    assert database.get_record(record_id) is not None
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) count FROM usage_records WHERE id=%s", (usage_id,)
            ).fetchone()["count"]
            == 0
        )


def test_retention_purges_expired_usage_and_location_but_keeps_recent(
    database: Database,
):
    device, _ = database.create_device("Field laptop")
    old = datetime.now(UTC) - timedelta(days=60)
    recent = datetime.now(UTC) - timedelta(days=2)
    old_usage, recent_usage = str(uuid4()), str(uuid4())
    old_location, recent_location = str(uuid4()), str(uuid4())
    with database.connect() as connection:
        for event_id, observed_at in ((old_usage, old), (recent_usage, recent)):
            connection.execute(
                """INSERT INTO usage_records(
                       id,device_id,observed_at,active_app,active_url,focused_seconds
                   ) VALUES (%s,%s,%s,'Browser','example.test',10)""",
                (event_id, device["id"], observed_at),
            )
        for event_id, recorded_at in (
            (old_location, old),
            (recent_location, recent),
        ):
            connection.execute(
                """INSERT INTO location_events(
                       id,device_id,recorded_at,latitude,longitude,accuracy_meters,created_at
                   ) VALUES (%s,%s,%s,31.5,74.3,12,%s)""",
                (event_id, device["id"], recorded_at, recorded_at),
            )

    RetentionService(database, FakeStorage(), 30).purge_expired()

    with database.connect() as connection:
        usage_ids = {
            row["id"]
            for row in connection.execute("SELECT id FROM usage_records").fetchall()
        }
        location_ids = {
            row["id"]
            for row in connection.execute("SELECT id FROM location_events").fetchall()
        }
    assert usage_ids == {recent_usage}
    assert location_ids == {recent_location}

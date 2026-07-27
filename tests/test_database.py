import sqlite3

from tracker_server.database import Database


def test_device_tokens_are_hashed_and_revocable(tmp_path):
    database = Database(tmp_path / "db.sqlite3")
    database.initialize()
    device, token = database.create_device("Pilot laptop")

    assert database.authenticate_device(token)["id"] == device["id"]
    assert token not in (tmp_path / "db.sqlite3").read_bytes().decode("latin1")

    database.set_device_enabled(device["id"], False)
    assert database.authenticate_device(token) is None


def test_activity_records_are_idempotent(tmp_path):
    database = Database(tmp_path / "db.sqlite3")
    database.initialize()
    device, _ = database.create_device("Pilot laptop")
    record = {
        "id": "9f84cd50-ee80-47de-9886-b33d49a5ecb2",
        "device_id": device["id"],
        "captured_at": "2026-07-27T10:00:00+00:00",
        "keyboard_events": 12,
        "mouse_clicks": 4,
        "mouse_distance": 1200,
        "active_app": "Editor",
        "agent_version": "0.1.0",
        "screenshot_path": "relative/image.jpg",
    }
    assert database.add_record(record) is True
    assert database.add_record(record) is False
    assert len(database.list_records(device["id"])) == 1


def test_existing_mvp_database_is_migrated_without_dropping_rows(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE devices (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 1, platform TEXT, created_at TEXT NOT NULL,
            last_seen_at TEXT, last_status TEXT
        );
        CREATE TABLE activity_records (
            id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(id),
            captured_at TEXT NOT NULL, received_at TEXT NOT NULL,
            keyboard_events INTEGER NOT NULL, mouse_clicks INTEGER NOT NULL,
            mouse_distance INTEGER NOT NULL, active_app TEXT, agent_version TEXT NOT NULL,
            screenshot_path TEXT NOT NULL, UNIQUE(device_id, captured_at)
        );
        INSERT INTO devices(id, name, token_hash, created_at)
        VALUES ('legacy-device', 'Legacy laptop', 'hash', '2026-01-01T00:00:00+00:00');
        """
    )
    connection.commit()
    connection.close()

    database = Database(path)
    database.initialize()

    assert database.get_device("legacy-device")["name"] == "Legacy laptop"
    connection = sqlite3.connect(path)
    device_columns = {row[1] for row in connection.execute("PRAGMA table_info(devices)")}
    record_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(activity_records)")
    }
    connection.close()
    assert "owner_user_id" in device_columns
    assert "storage_version_id" in record_columns

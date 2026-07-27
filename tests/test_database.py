from tracker_server.database import Database
from tracker_server.migrations import MIGRATIONS, apply_migrations


def test_device_tokens_are_hashed_and_revocable(database: Database):
    device, token = database.create_device("Pilot laptop")

    assert database.authenticate_device(token)["id"] == device["id"]
    with database.connect() as connection:
        stored_hash = connection.execute(
            "SELECT token_hash FROM devices WHERE id = %s", (device["id"],)
        ).fetchone()["token_hash"]
    assert stored_hash != token

    database.set_device_enabled(device["id"], False)
    assert database.authenticate_device(token) is None


def test_activity_records_are_idempotent(database: Database):
    device, _ = database.create_device("Pilot laptop")
    record = {
        "id": "9f84cd50-ee80-47de-9886-b33d49a5ecb2",
        "device_id": device["id"],
        "captured_at": "2026-07-27T10:00:00+00:00",
        "keyboard_events": 12,
        "mouse_clicks": 4,
        "mouse_distance": 1200,
        "active_app": "Editor",
        "agent_version": "0.3.0",
        "screenshot_path": "relative/image.jpg",
    }
    assert database.add_record(record) is True
    assert database.add_record(record) is False
    assert len(database.list_records(device["id"])) == 1


def test_postgresql_schema_uses_native_types_and_versioned_migrations(database: Database):
    with database.connect() as connection:
        apply_migrations(connection)
        applied = connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
        columns = connection.execute(
            """SELECT column_name, data_type FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'activity_records'"""
        ).fetchall()

    assert [(row["version"], row["name"]) for row in applied] == [
        (migration.version, migration.name) for migration in MIGRATIONS
    ]
    types = {row["column_name"]: row["data_type"] for row in columns}
    assert types["id"] == "uuid"
    assert types["captured_at"] == "timestamp with time zone"
    assert types["mouse_distance"] == "bigint"


def test_users_can_belong_to_multiple_projects_and_devices_stay_partitioned(
    database: Database,
):
    admin = database.bootstrap_admin("admin@example.com", "hash")
    user_id = "11111111-1111-1111-1111-111111111111"
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO users(id, email, password_hash, role, enabled, created_at)
               VALUES (%s, 'dev@example.com', 'hash', 'member', TRUE, '2026-01-01T00:00:00+00:00')""",
            (user_id,),
        )
    alpha = database.create_project("Alpha", "First", admin["id"])
    beta = database.create_project("Beta", "Second", admin["id"])
    database.add_project_member(alpha["id"], user_id)
    database.add_project_member(beta["id"], user_id)
    database.create_device("Alpha laptop", user_id, alpha["id"])
    database.create_device("Beta laptop", user_id, beta["id"])

    assert {project["name"] for project in database.list_projects(user_id)} == {
        "Alpha",
        "Beta",
    }
    assert [
        device["name"] for device in database.list_devices(user_id, alpha["id"])
    ] == ["Alpha laptop"]

import uuid
from datetime import UTC, datetime, timedelta

from api.database import Database


def _setup(database: Database):
    admin = database.bootstrap_admin("admin@example.com", "hash")
    project = database.create_project("Alpha", "", admin["id"])
    task = database.create_task(project["id"], "Review AI output", "", admin["id"])
    _, token = database.create_device("Laptop", admin["id"], project["id"])
    return admin, project, task, database.authenticate_device(token)


def test_heartbeat_state_creates_pauses_resumes_and_stops_session(database: Database):
    _, _, task, device = _setup(database)

    active = database.sync_work_session(device, "active", task["id"])
    paused = database.sync_work_session(device, "paused", task["id"])
    resumed = database.sync_work_session(device, "active", task["id"])
    stopped = database.sync_work_session(device, "stopped", None)

    assert active["id"] == paused["id"] == resumed["id"]
    assert paused["status"] == "paused"
    assert resumed["status"] == "active"
    assert stopped is None
    assert database.get_work_session(active["id"])["status"] == "stopped"
    with database.connect() as connection:
        segments = connection.execute(
            "SELECT * FROM work_session_segments WHERE session_id = %s",
            (active["id"],),
        ).fetchall()
    assert len(segments) == 2
    assert all(segment["ended_at"] for segment in segments)


def test_changing_task_stops_old_session_and_preserves_capture_attribution(
    database: Database,
):
    admin, project, first_task, device = _setup(database)
    second_task = database.create_task(project["id"], "Run tests", "", admin["id"])
    first = database.sync_work_session(device, "active", first_task["id"])
    second = database.sync_work_session(device, "active", second_task["id"])

    assert first["id"] != second["id"]
    assert database.get_work_session(first["id"])["status"] == "stopped"
    assert second["task_id"] == second_task["id"]

    record_id = "9f84cd50-ee80-47de-9886-b33d49a5ecb2"
    assert database.add_record(
        {
            "id": record_id,
            "device_id": device["id"],
            "user_id": second["user_id"],
            "project_id": second["project_id"],
            "task_id": second["task_id"],
            "session_id": second["id"],
            "captured_at": "2026-07-27T10:00:00+00:00",
            "keyboard_events": 0,
            "mouse_clicks": 0,
            "mouse_distance": 0,
            "agent_version": "0.3.0",
            "screenshot_path": "capture.jpg",
        }
    )
    record = database.get_record(record_id)
    assert record["project_id"] == project["id"]
    assert record["task_id"] == second_task["id"]
    assert record["session_id"] == second["id"]


def test_task_from_another_project_is_rejected(database: Database):
    admin, _, _, device = _setup(database)
    other = database.create_project("Beta", "", admin["id"])
    task = database.create_task(other["id"], "Wrong project", "", admin["id"])

    try:
        database.sync_work_session(device, "active", task["id"])
    except ValueError as exc:
        assert "device's project" in str(exc)
    else:
        raise AssertionError("cross-project task should be rejected")


def _event(database, device, status, task_id, when, **values):
    return database.sync_work_session(
        device,
        status,
        task_id,
        event_id=values.pop("event_id", str(uuid.uuid4())),
        observed_at=when,
        heartbeat_interval_seconds=values.pop("heartbeat_interval_seconds", 60),
        **values,
    )


def test_idle_transition_deducts_time_since_last_input(database: Database):
    _, _, task, device = _setup(database)
    started = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)
    session = _event(database, device, "active", task["id"], started)
    for minute in range(1, 31):
        _event(
            database,
            device,
            "active",
            task["id"],
            started + timedelta(minutes=minute),
        )

    # Detected after 30 minutes with the last real input one second after start.
    _event(
        database,
        device,
        "paused",
        task["id"],
        started + timedelta(seconds=1801),
        idle_seconds=1800,
    )

    with database.connect() as connection:
        segment = connection.execute(
            "SELECT * FROM work_session_segments WHERE session_id = %s",
            (session["id"],),
        ).fetchone()
    assert datetime.fromisoformat(segment["ended_at"]) == started + timedelta(seconds=1)


def test_state_event_replay_is_idempotent(database: Database):
    _, _, task, device = _setup(database)
    observed = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)
    event_id = str(uuid.uuid4())

    first = _event(database, device, "active", task["id"], observed, event_id=event_id)
    duplicate = _event(
        database, device, "active", task["id"], observed, event_id=event_id
    )

    assert duplicate["id"] == first["id"]
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) AS count FROM agent_state_events WHERE id = %s",
                (event_id,),
            ).fetchone()["count"]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) AS count FROM work_session_segments WHERE session_id = %s",
                (first["id"],),
            ).fetchone()["count"]
            == 1
        )


def test_restart_gap_caps_previous_offline_session(database: Database):
    _, _, task, device = _setup(database)
    started = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)
    old = _event(database, device, "active", task["id"], started)
    _event(database, device, "active", task["id"], started + timedelta(minutes=1))

    resumed = _event(
        database, device, "active", task["id"], started + timedelta(hours=4)
    )

    assert resumed["id"] != old["id"]
    stopped = database.get_work_session(old["id"])
    assert stopped["status"] == "stopped"
    assert datetime.fromisoformat(stopped["ended_at"]) == started + timedelta(minutes=2)

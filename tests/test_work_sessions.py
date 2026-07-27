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

import re
import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from api.config import Settings
from api.main import create_app


def test_field_timer_replays_offline_actions_in_order_and_idempotently(
    tmp_path, postgres_url
):
    settings = Settings(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024,
        retention_days=30,
        admin_email="field-admin@example.test",
        database_url=postgres_url,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        login_page = client.get("/login")
        csrf = re.search(r'name="csrf" value="([^"]+)"', login_page.text).group(1)
        assert (
            client.post(
                "/login",
                data={
                    "email": settings.admin_email,
                    "password": settings.admin_password,
                    "csrf": csrf,
                },
                follow_redirects=False,
            ).status_code
            == 303
        )
        database = app.state.database
        admin = database.get_user_by_email(settings.admin_email)
        project = database.create_project("Field project", "", admin["id"])
        csrf = re.search(
            r'name="csrf" value="([^"]+)"', client.get("/field").text
        ).group(1)
        headers = {"X-CSRF-Token": csrf}
        started = datetime.now(UTC) - timedelta(minutes=1)
        events = []
        for offset, action in enumerate(("start", "pause", "resume", "stop")):
            payload = {
                "event_id": str(uuid.uuid4()),
                "observed_at": (started + timedelta(seconds=offset * 10)).isoformat(),
                "action": action,
                "project_id": project["id"] if action == "start" else None,
            }
            response = client.post("/field/timer", headers=headers, json=payload)
            assert response.status_code == 201, response.text
            events.append(payload)

        duplicate = client.post("/field/timer", headers=headers, json=events[-1])
        assert duplicate.status_code == 201
        sessions = database.list_work_sessions(admin["id"], project["id"])
        assert len(sessions) == 1
        assert sessions[0]["status"] == "stopped"
        with database.connect() as connection:
            state_count = connection.execute(
                "SELECT COUNT(*) count FROM agent_state_events WHERE device_id=%s",
                (sessions[0]["device_id"],),
            ).fetchone()["count"]
            segment_count = connection.execute(
                "SELECT COUNT(*) count FROM work_session_segments WHERE session_id=%s",
                (sessions[0]["id"],),
            ).fetchone()["count"]
        assert state_count == 4
        assert segment_count == 2

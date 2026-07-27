from fastapi.testclient import TestClient

from tracker_server.config import Settings
from tracker_server.main import create_app


def test_agent_authentication_and_idempotent_upload(tmp_path, postgres_url):
    settings = Settings(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024 * 1024,
        retention_days=30,
        database_url=postgres_url,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        database = app.state.database
        device, token = database.create_device("Test laptop")
        payload = {
            "record_id": "9f84cd50-ee80-47de-9886-b33d49a5ecb2",
            "captured_at": "2026-07-27T10:00:00+00:00",
            "keyboard_events": "12",
            "mouse_clicks": "3",
            "mouse_distance": "900",
            "active_app": "Editor",
            "agent_version": "0.1.0",
        }
        unauthenticated = client.post(
            "/api/v1/activity",
            data=payload,
            files={"screenshot_file": ("capture.jpg", b"\xff\xd8\xfffake", "image/jpeg")},
        )
        assert unauthenticated.status_code == 401

        headers = {"Authorization": f"Bearer {token}"}
        created = client.post(
            "/api/v1/activity",
            headers=headers,
            data=payload,
            files={"screenshot_file": ("capture.jpg", b"\xff\xd8\xfffake", "image/jpeg")},
        )
        duplicate = client.post(
            "/api/v1/activity",
            headers=headers,
            data=payload,
            files={"screenshot_file": ("capture.jpg", b"\xff\xd8\xfffake", "image/jpeg")},
        )
        assert created.status_code == 201
        assert duplicate.json()["status"] == "duplicate"
        assert len(database.list_records(device["id"])) == 1

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from api.config import Settings
from api.main import create_app


def test_agent_ingest_rejects_clock_payload_file_and_session_edges(
    tmp_path, postgres_url
):
    settings = Settings(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=16,
        retention_days=30,
        admin_email="edges@example.test",
        database_url=postgres_url,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        database = app.state.database
        first, first_token = database.create_device("First")
        _, second_token = database.create_device("Second")
        first_headers = {"Authorization": f"Bearer {first_token}"}
        second_headers = {"Authorization": f"Bearer {second_token}"}
        base = {
            "platform": "Test",
            "status": "active",
            "heartbeat_interval_seconds": 60,
        }

        naive = client.post(
            "/api/v1/heartbeat",
            headers=first_headers,
            json={**base, "observed_at": "2026-09-08T10:00:00"},
        )
        assert naive.status_code == 422
        future = client.post(
            "/api/v1/heartbeat",
            headers=first_headers,
            json={
                **base,
                "observed_at": (datetime.now(UTC) + timedelta(minutes=6)).isoformat(),
            },
        )
        assert future.status_code == 422
        old = client.post(
            "/api/v1/heartbeat",
            headers=first_headers,
            json={
                **base,
                "observed_at": (datetime.now(UTC) - timedelta(days=91)).isoformat(),
            },
        )
        assert old.status_code == 422

        active = client.post("/api/v1/heartbeat", headers=first_headers, json=base)
        assert active.status_code == 200
        session_id = active.json()["session_id"]
        payload = {
            "record_id": "not-a-uuid",
            "captured_at": datetime.now(UTC).isoformat(),
            "keyboard_events": "0",
            "mouse_clicks": "0",
            "mouse_distance": "0",
            "active_app": "Editor",
            "agent_version": "test",
        }
        bad_id = client.post(
            "/api/v1/activity",
            headers=first_headers,
            data=payload,
            files={"screenshot_file": ("capture.jpg", b"\xff\xd8\xffok", "image/jpeg")},
        )
        assert bad_id.status_code == 422
        payload["record_id"] = "4a6edcff-62af-43ab-97a4-73d69de6584c"
        empty = client.post(
            "/api/v1/activity",
            headers=first_headers,
            data=payload,
            files={"screenshot_file": ("capture.jpg", b"", "image/jpeg")},
        )
        assert empty.status_code == 413
        oversized = client.post(
            "/api/v1/activity",
            headers=first_headers,
            data=payload,
            files={
                "screenshot_file": (
                    "capture.jpg",
                    b"\xff\xd8\xff" + b"x" * 20,
                    "image/jpeg",
                )
            },
        )
        assert oversized.status_code == 413
        invalid_image = client.post(
            "/api/v1/activity",
            headers=first_headers,
            data=payload,
            files={"screenshot_file": ("capture.jpg", b"not-image", "image/jpeg")},
        )
        assert invalid_image.status_code == 415

        payload["session_id"] = session_id
        cross_device = client.post(
            "/api/v1/activity",
            headers=second_headers,
            data=payload,
            files={"screenshot_file": ("capture.jpg", b"\xff\xd8\xffok", "image/jpeg")},
        )
        assert cross_device.status_code == 422
        assert not database.list_records(first["id"])

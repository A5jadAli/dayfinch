from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from api.config import Settings
from api.main import create_app


def test_agent_authentication_and_idempotent_upload(tmp_path, postgres_url):
    settings = Settings(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024 * 1024,
        retention_days=30,
        database_url=postgres_url,
        metrics_bearer_token="m" * 40,
    )
    app = create_app(settings)
    assert app.version == "0.6.0"
    with TestClient(app) as client:
        login_page = client.get("/login")
        assert login_page.headers["x-request-id"]
        assert login_page.headers["cache-control"] == "private, no-store"
        assert login_page.headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in login_page.headers["content-security-policy"]
        assert f"/static/app.css?v={app.state.asset_version}" in login_page.text
        assert f"/static/app.js?v={app.state.asset_version}" in login_page.text
        service_worker = client.get("/service-worker.js")
        assert service_worker.status_code == 200
        assert app.state.asset_version in service_worker.text
        assert "__DAYFINCH_ASSET_VERSION__" not in service_worker.text
        assert service_worker.headers["cache-control"] == (
            "no-cache, no-store, must-revalidate"
        )
        assert client.get("/livez").json() == {"status": "alive"}
        assert client.get("/readyz").json() == {"status": "ready"}
        assert client.get("/metrics").status_code == 404
        metrics = client.get(
            "/metrics", headers={"Authorization": f"Bearer {'m' * 40}"}
        )
        assert metrics.status_code == 200
        assert "dayfinch_http_requests_total" in metrics.text
        assert "dayfinch_readiness 1" in metrics.text
        assert "dayfinch_metrics_collection_success 1" in metrics.text
        assert 'dayfinch_queue_backlog{queue="slack_outbox"} 0' in metrics.text
        assert (
            client.get("/health", headers={"host": "untrusted.test"}).status_code == 400
        )
        database = app.state.database
        admin = database.get_user_by_email(settings.admin_email)
        project = database.create_project("Agent API project", "", admin["id"])
        device, token = database.create_device(
            "Test laptop", admin["id"], project["id"]
        )
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
            files={
                "screenshot_file": ("capture.jpg", b"\xff\xd8\xfffake", "image/jpeg")
            },
        )
        assert unauthenticated.status_code == 401

        headers = {"Authorization": f"Bearer {token}"}
        legacy_heartbeat = client.post(
            "/api/v1/heartbeat",
            headers=headers,
            json={"platform": "Linux", "status": "active"},
        )
        assert legacy_heartbeat.status_code == 200
        payload["captured_at"] = datetime.now(UTC).isoformat()

        created = client.post(
            "/api/v1/activity",
            headers=headers,
            data=payload,
            files={
                "screenshot_file": ("capture.jpg", b"\xff\xd8\xfffake", "image/jpeg")
            },
        )
        duplicate = client.post(
            "/api/v1/activity",
            headers=headers,
            data=payload,
            files={
                "screenshot_file": ("capture.jpg", b"\xff\xd8\xfffake", "image/jpeg")
            },
        )
        assert created.status_code == 201
        assert duplicate.json()["status"] == "duplicate"
        assert len(database.list_records(device["id"])) == 1


def test_native_location_is_attributed_to_its_active_session(tmp_path, postgres_url):
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
        owner = database.get_user_by_email(settings.admin_email)
        project = database.create_project("Mobile project", "", owner["id"])
        database.add_project_member(project["id"], owner["id"])
        _, token = database.create_device(
            "Work phone", owner["id"], project["id"], tracker_kind="mobile"
        )
        headers = {"Authorization": f"Bearer {token}"}
        outside_session = client.post(
            "/api/v1/location",
            headers=headers,
            json={
                "event_id": str(uuid4()),
                "recorded_at": datetime.now(UTC).isoformat(),
                "latitude": 31.5204,
                "longitude": 74.3587,
                "accuracy_meters": 12,
            },
        )
        assert outside_session.status_code == 409
        started_at = datetime.now(UTC)
        heartbeat = client.post(
            "/api/v1/heartbeat",
            headers=headers,
            json={
                "event_id": str(uuid4()),
                "observed_at": started_at.isoformat(),
                "status": "active",
                "project_id": project["id"],
                "platform": "Dayfinch Mobile android",
                "transition": True,
            },
        )
        assert heartbeat.status_code == 200
        location = client.post(
            "/api/v1/location",
            headers=headers,
            json={
                "event_id": str(uuid4()),
                "recorded_at": datetime.now(UTC).isoformat(),
                "latitude": 31.5204,
                "longitude": 74.3587,
                "accuracy_meters": 12,
            },
        )

        assert location.status_code == 201
        events = database.list_locations(owner["id"])
        assert events[0]["session_id"] == heartbeat.json()["session_id"]

        usage = client.post(
            "/api/v1/usage",
            headers=headers,
            json={
                "event_id": str(uuid4()),
                "observed_at": datetime.now(UTC).isoformat(),
                "active_app": "Should not be accepted",
                "active_url": "example.test",
                "focused_seconds": 1,
            },
        )
        assert usage.status_code == 403
        assert usage.json()["detail"] == (
            "Activity collection requires a desktop tracker"
        )

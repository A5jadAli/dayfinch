from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from api.config import Settings
from api.main import create_app
from api.security import hash_password


def _settings(tmp_path, postgres_url):
    return Settings(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024 * 1024,
        retention_days=30,
        admin_email="usage-admin@example.test",
        database_url=postgres_url,
    )


def test_usage_replay_is_idempotent_private_and_session_attributed(
    tmp_path, postgres_url
):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        admin = database.get_user_by_email("usage-admin@example.test")
        _, invite = database.create_invitation(
            "usage-member@example.test", admin["id"], 24
        )
        member = database.accept_invitation(
            invite, hash_password("member password long")
        )
        project = database.create_project("Usage project", "", admin["id"])
        database.add_project_member(project["id"], member["id"])
        task = database.create_task(project["id"], "Browse docs", "", admin["id"])
        _, token = database.create_device("Usage laptop", member["id"], project["id"])
        observed = datetime.now(UTC) - timedelta(seconds=20)
        heartbeat = client.post(
            "/api/v1/heartbeat",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "platform": "Test",
                "event_id": "f8d6b983-1f89-4e65-af90-4a031869f210",
                "observed_at": observed.isoformat(),
                "status": "active",
                "project_id": project["id"],
                "task_id": task["id"],
                "heartbeat_interval_seconds": 60,
            },
        )
        assert heartbeat.status_code == 200
        payload = {
            "event_id": "2b8877f0-880a-43e5-b879-8785d1adf818",
            "observed_at": (observed + timedelta(seconds=10)).isoformat(),
            "active_app": "Google Chrome",
            "active_url": "https://www.docs.example.test/private?q=secret#fragment",
            "focused_seconds": 10,
        }
        headers = {"Authorization": f"Bearer {token}"}
        assert (
            client.post("/api/v1/usage", headers=headers, json=payload).status_code
            == 201
        )
        duplicate = client.post("/api/v1/usage", headers=headers, json=payload)
        assert duplicate.json()["status"] == "duplicate"

        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM usage_records WHERE id=%s", (payload["event_id"],)
            ).fetchone()
        assert row["active_url"] == "docs.example.test"
        assert row["session_id"] == heartbeat.json()["session_id"]
        assert row["project_id"] == project["id"]
        assert row["task_id"] == task["id"]
        summary = database.usage_summary("active_url", member["id"])
        assert summary[0]["name"] == "docs.example.test"
        assert summary[0]["seconds"] == 10


def test_usage_policy_removes_application_and_domain(tmp_path, postgres_url):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        admin = database.get_user_by_email("usage-admin@example.test")
        project = database.create_project("Policy usage project", "", admin["id"])
        _, token = database.create_device(
            "Policy usage laptop", admin["id"], project["id"]
        )
        database.update_organization_settings(
            {"track_apps": False, "track_urls": False}
        )
        blocked = client.post(
            "/api/v1/usage",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "event_id": "7102187a-11c1-4f99-a3bb-85c4d4f078a1",
                "observed_at": datetime.now(UTC).isoformat(),
                "active_app": "Must not persist",
                "focused_seconds": 10,
            },
        )
        assert blocked.status_code == 409
        heartbeat = client.post(
            "/api/v1/heartbeat",
            headers={"Authorization": f"Bearer {token}"},
            json={"platform": "Test", "status": "active"},
        )
        assert heartbeat.status_code == 200
        event_id = "6102187a-11c1-4f99-a3bb-85c4d4f078a1"
        response = client.post(
            "/api/v1/usage",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "event_id": event_id,
                "observed_at": datetime.now(UTC).isoformat(),
                "active_app": "Should not persist",
                "active_url": "private.example/path",
                "focused_seconds": 10,
            },
        )
        assert response.status_code == 201
        with database.connect() as connection:
            row = connection.execute(
                "SELECT active_app,active_url FROM usage_records WHERE id=%s",
                (event_id,),
            ).fetchone()
        assert row["active_app"] is None
        assert row["active_url"] is None

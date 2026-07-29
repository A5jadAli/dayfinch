"""Server-side handling of website domain and suspected-automation fields."""

from __future__ import annotations

from fastapi.testclient import TestClient

from api.config import Settings
from api.main import create_app
from api.security import hash_password


def _settings(tmp_path, postgres_url) -> Settings:
    return Settings(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024 * 1024,
        retention_days=30,
        admin_email="admin@example.test",
        database_url=postgres_url,
    )


def _enrolled_device(database):
    admin = database.get_user_by_email("admin@example.test")
    _, invite = database.create_invitation("member@example.test", admin["id"], 24)
    member = database.accept_invitation(invite, hash_password("member password long"))
    _, token = database.create_device("Laptop", member["id"])
    return token


def _post_activity(client, token, record_id, **fields):
    payload = {
        "record_id": record_id,
        "captured_at": "2026-07-27T10:00:00+00:00",
        "keyboard_events": "5",
        "mouse_clicks": "3",
        "mouse_distance": "120",
        "active_app": "Google Chrome",
        "agent_version": "0.5.0",
        "focused_seconds": "600",
        "interactive_seconds": "600",
    }
    payload.update(fields)
    return client.post(
        "/api/v1/activity",
        headers={"Authorization": f"Bearer {token}"},
        data=payload,
        files={"screenshot_file": ("capture.jpg", b"\xff\xd8\xfffake", "image/jpeg")},
    )


def test_website_domain_is_stored(tmp_path, postgres_url):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        token = _enrolled_device(database)
        rid = "6f9619ff-8b86-d011-b42d-00cf4fc964ff"

        assert (
            _post_activity(
                client,
                token,
                rid,
                active_url="https://www.github.com/private/repo?token=secret",
            ).status_code
            == 201
        )

        record = database.get_record(rid)
        assert record["active_url"] == "github.com"
        assert record["automation_suspected"] is False


def test_suspected_automation_zeroes_interactive_time_server_side(
    tmp_path, postgres_url
):
    """Even if the agent claims interaction, faked input must not be counted."""
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        token = _enrolled_device(database)
        rid = "6f9619ff-8b86-d011-b42d-00cf4fc964aa"

        response = _post_activity(
            client,
            token,
            rid,
            interactive_seconds="600",
            automation_suspected="true",
        )
        assert response.status_code == 201

        record = database.get_record(rid)
        assert record["automation_suspected"] is True
        assert record["interactive_seconds"] == 0
        # The raw screenshot and counts are kept as evidence; only the derived
        # interaction time is neutralized.
        assert record["keyboard_events"] == 5

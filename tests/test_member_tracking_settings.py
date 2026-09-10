from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from api.config import Settings
from api.main import create_app
from api.security import hash_password


def _settings(tmp_path, postgres_url: str) -> Settings:
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


def _csrf(response) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert match, response.text
    return match.group(1)


def _login(client: TestClient, email: str, password: str) -> None:
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf": _csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _member(database, email: str = "member@example.test") -> dict:
    admin = database.get_user_by_email("admin@example.test")
    _, token = database.create_invitation(email, admin["id"], 24)
    return database.accept_invitation(
        token, hash_password("member password long enough")
    )


def _override(**values):
    return {
        "screenshot_frequency": None,
        "screenshot_blur": None,
        "track_apps": None,
        "track_urls": None,
        "allowed_apps": None,
        "idle_timeout_minutes": None,
        "allow_screenshot_delete": None,
        **values,
    }


def test_member_tracking_settings_inherit_and_follow_changed_defaults(
    tmp_path, postgres_url
):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app):
        database = app.state.database
        admin = database.get_user_by_email("admin@example.test")
        member = _member(database)

        inherited = database.effective_tracking_settings(member["id"])
        assert inherited["screenshot_frequency"] == 2
        assert inherited["track_apps"] is True

        database.update_member_tracking_settings(
            member["id"],
            _override(
                screenshot_frequency=0,
                screenshot_blur=True,
                track_apps=False,
                track_urls=False,
                idle_timeout_minutes=7,
                allow_screenshot_delete=False,
            ),
            admin["id"],
        )
        effective = database.effective_tracking_settings(member["id"])
        assert effective == {
            "screenshot_frequency": 0,
            "screenshot_blur": True,
            "track_apps": False,
            "track_urls": False,
            "allowed_apps": "all",
            "idle_timeout_minutes": 7,
            "allow_screenshot_delete": False,
        }

        database.update_organization_settings(
            {"screenshot_frequency": 3, "track_apps": False}
        )
        assert database.effective_tracking_settings(member["id"]) == effective

        database.update_member_tracking_settings(member["id"], _override(), admin["id"])
        reset = database.effective_tracking_settings(member["id"])
        assert reset["screenshot_frequency"] == 3
        assert reset["track_apps"] is False
        with database.connect() as connection:
            assert (
                connection.execute(
                    "SELECT 1 FROM user_tracking_settings WHERE user_id=%s",
                    (member["id"],),
                ).fetchone()
                is None
            )


def test_member_policy_is_enforced_by_configuration_upload_and_deletion(
    tmp_path, postgres_url
):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        admin = database.get_user_by_email("admin@example.test")
        member = _member(database)
        project = database.create_project("Private work", "", admin["id"])
        database.add_project_member(project["id"], member["id"])
        device, token = database.create_device(
            "Member laptop", member["id"], project["id"]
        )
        headers = {"Authorization": f"Bearer {token}"}
        database.update_member_tracking_settings(
            member["id"],
            _override(
                screenshot_frequency=0,
                screenshot_blur=True,
                track_apps=False,
                track_urls=False,
                idle_timeout_minutes=5,
                allow_screenshot_delete=False,
            ),
            admin["id"],
        )

        configuration = client.get("/api/v1/configuration", headers=headers)
        assert configuration.status_code == 200
        assert {
            key: configuration.json()[key]
            for key in (
                "screenshot_frequency",
                "screenshot_blur",
                "track_apps",
                "track_urls",
                "allowed_apps",
                "idle_timeout_minutes",
            )
        } == {
            "screenshot_frequency": 0,
            "screenshot_blur": True,
            "track_apps": False,
            "track_urls": False,
            "allowed_apps": "all",
            "idle_timeout_minutes": 5,
        }

        now = datetime.now(UTC)
        database.sync_work_session(
            device,
            "active",
            None,
            project["id"],
            observed_at=now - timedelta(seconds=20),
        )
        payload = {
            "record_id": str(uuid.uuid4()),
            "captured_at": now.isoformat(),
            "keyboard_events": "2",
            "mouse_clicks": "1",
            "mouse_distance": "20",
            "active_app": "Private editor",
            "active_url": "https://private.example/path",
            "agent_version": "0.6.0",
            "screenshot_blurred": "true",
        }
        denied = client.post(
            "/api/v1/activity",
            headers=headers,
            data=payload,
            files={"screenshot_file": ("capture.jpg", b"\xff\xd8\xffok", "image/jpeg")},
        )
        assert denied.status_code == 403

        database.update_member_tracking_settings(
            member["id"],
            _override(
                screenshot_frequency=1,
                screenshot_blur=True,
                track_apps=False,
                track_urls=False,
                idle_timeout_minutes=5,
                allow_screenshot_delete=False,
            ),
            admin["id"],
        )
        accepted = client.post(
            "/api/v1/activity",
            headers=headers,
            data=payload,
            files={"screenshot_file": ("capture.jpg", b"\xff\xd8\xffok", "image/jpeg")},
        )
        assert accepted.status_code == 201
        record = database.get_record(payload["record_id"])
        assert record["active_app"] is None
        assert record["active_url"] is None
        assert record["screenshot_blurred"] is True

        _login(client, "member@example.test", "member password long enough")
        detail = client.get(f"/devices/{device['id']}")
        blocked = client.post(
            f"/screenshots/{payload['record_id']}/delete",
            data={"csrf": _csrf(detail)},
            follow_redirects=False,
        )
        assert blocked.status_code == 403
        assert database.get_record(payload["record_id"]) is not None


def test_owner_can_manage_member_policy_and_member_cannot(tmp_path, postgres_url):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        member = _member(database)
        _login(client, "admin@example.test", "correct horse battery staple")

        page = client.get("/settings/member-tracking?search=member")
        assert page.status_code == 200
        assert "member@example.test" in page.text
        saved = client.post(
            f"/settings/member-tracking/{member['id']}",
            data={
                "csrf": _csrf(page),
                "screenshot_frequency": "0",
                "screenshot_blur": "on",
                "track_apps": "off",
                "track_urls": "inherit",
                "allowed_apps": "desktop_only",
                "idle_timeout_minutes": "9",
                "allow_screenshot_delete": "off",
            },
            follow_redirects=False,
        )
        assert saved.status_code == 303
        effective = database.effective_tracking_settings(member["id"])
        assert effective["screenshot_frequency"] == 0
        assert effective["screenshot_blur"] is True
        assert effective["track_apps"] is False
        assert effective["track_urls"] is True
        assert effective["allowed_apps"] == "desktop_only"
        assert effective["idle_timeout_minutes"] == 9
        assert effective["allow_screenshot_delete"] is False
        with database.connect() as connection:
            audit = connection.execute(
                """SELECT action,target_type,target_id FROM audit_events
                   WHERE action='tracking_policy.updated'"""
            ).fetchone()
        assert audit["target_type"] == "user"
        assert audit["target_id"] == member["id"]

        client.cookies.clear()
        _login(client, "member@example.test", "member password long enough")
        assert client.get("/settings/member-tracking").status_code == 403
        forbidden = client.post(
            f"/settings/member-tracking/{member['id']}",
            data={
                "csrf": _csrf(client.get("/")),
                "screenshot_frequency": "inherit",
                "screenshot_blur": "inherit",
                "track_apps": "inherit",
                "track_urls": "inherit",
                "allowed_apps": "inherit",
                "idle_timeout_minutes": "",
                "allow_screenshot_delete": "inherit",
            },
        )
        assert forbidden.status_code == 403


def test_member_tracking_settings_validate_edges_and_bound_listing(
    tmp_path, postgres_url
):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app):
        database = app.state.database
        admin = database.get_user_by_email("admin@example.test")
        member = _member(database)
        with pytest.raises(ValueError, match="between 0 and 3"):
            database.update_member_tracking_settings(
                member["id"], _override(screenshot_frequency=4), admin["id"]
            )
        with pytest.raises(ValueError, match="between 1 and 1440"):
            database.update_member_tracking_settings(
                member["id"], _override(idle_timeout_minutes=0), admin["id"]
            )
        with pytest.raises(ValueError, match="switches"):
            database.update_member_tracking_settings(
                member["id"], _override(track_apps="off"), admin["id"]
            )
        rows, total = database.list_member_tracking_settings(
            "member", limit=500, offset=-10
        )
        assert total == 1
        assert [row["id"] for row in rows] == [member["id"]]

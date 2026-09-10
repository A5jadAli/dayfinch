from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

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
    client.cookies.clear()
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf": _csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _member(database) -> dict:
    owner = database.get_user_by_email("admin@example.test")
    _, token = database.create_invitation("member@example.test", owner["id"], 24)
    return database.accept_invitation(
        token, hash_password("member password long enough")
    )


def _member_policy(allowed_apps: str | None) -> dict:
    return {
        "screenshot_frequency": None,
        "screenshot_blur": None,
        "track_apps": None,
        "track_urls": None,
        "allowed_apps": allowed_apps,
        "idle_timeout_minutes": None,
        "allow_screenshot_delete": None,
    }


def test_desktop_only_hides_and_rejects_web_and_field_timers_but_allows_agent(
    tmp_path, postgres_url
):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        owner = database.get_user_by_email("admin@example.test")
        project = database.create_project("Engineering", "", owner["id"])
        database.add_project_member(project["id"], owner["id"])
        database.update_organization_settings({"allowed_apps": "desktop_only"})
        _login(client, owner["email"], "correct horse battery staple")

        dashboard = client.get("/")
        assert "Desktop tracking required" in dashboard.text
        assert 'action="/timer/start"' not in dashboard.text
        denied_web = client.post(
            "/timer/start",
            data={"project_id": project["id"], "csrf": _csrf(dashboard)},
        )
        assert denied_web.status_code == 403
        assert denied_web.json()["detail"] == "Desktop tracking is required by policy"

        field = client.get("/field")
        assert "Desktop tracking is required by policy" in field.text
        assert 'action="/timer/start"' not in field.text
        denied_field = client.post(
            "/field/timer",
            headers={"X-CSRF-Token": _csrf(field)},
            json={
                "event_id": str(uuid.uuid4()),
                "observed_at": datetime.now(UTC).isoformat(),
                "action": "start",
                "project_id": project["id"],
            },
        )
        assert denied_field.status_code == 403
        assert denied_field.json()["detail"] == (
            "Desktop tracking is required by policy"
        )

        database.create_geofence(
            "Office", project["id"], 24.8607, 67.0011, 100, "start", "stop"
        )
        location = client.post(
            "/field/location",
            headers={"X-CSRF-Token": _csrf(field)},
            json={
                "event_id": str(uuid.uuid4()),
                "recorded_at": datetime.now(UTC).isoformat(),
                "latitude": 24.8607,
                "longitude": 67.0011,
                "accuracy_meters": 5,
                "event_type": "position",
            },
        )
        assert location.status_code == 201
        assert location.json()["event_type"] == "enter"
        assert database.active_timer(owner["id"]) is None

        device, token = database.create_device(
            "Desktop workstation", owner["id"], project["id"]
        )
        heartbeat = client.post(
            "/api/v1/heartbeat",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "event_id": str(uuid.uuid4()),
                "observed_at": datetime.now(UTC).isoformat(),
                "status": "active",
                "project_id": project["id"],
                "platform": "Linux",
                "transition": True,
            },
        )
        assert heartbeat.status_code == 200
        assert (
            database.get_work_session(heartbeat.json()["session_id"])["device_id"]
            == device["id"]
        )

        _, mobile_token = database.create_device(
            "Work phone", owner["id"], project["id"], tracker_kind="mobile"
        )
        denied_mobile = client.post(
            "/api/v1/heartbeat",
            headers={"Authorization": f"Bearer {mobile_token}"},
            json={
                "event_id": str(uuid.uuid4()),
                "observed_at": datetime.now(UTC).isoformat(),
                "status": "active",
                "project_id": project["id"],
                "platform": "Dayfinch Mobile android",
                "transition": True,
            },
        )
        assert denied_mobile.status_code == 422
        assert denied_mobile.json()["detail"] == (
            "Desktop tracking is required by policy"
        )


def test_policy_change_closes_running_native_mobile_session(tmp_path, postgres_url):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app):
        database = app.state.database
        owner = database.get_user_by_email("admin@example.test")
        project = database.create_project("Mobile operations", "", owner["id"])
        database.add_project_member(project["id"], owner["id"])
        device, _ = database.create_device(
            "Work phone", owner["id"], project["id"], tracker_kind="mobile"
        )
        session = database.sync_work_session(
            device, "active", None, project["id"], transition=True
        )

        database.update_organization_settings({"allowed_apps": "desktop_only"})

        assert database.get_work_session(session["id"])["status"] == "stopped"
        with database.connect() as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) count FROM work_session_segments "
                    "WHERE session_id=%s AND ended_at IS NULL",
                    (session["id"],),
                ).fetchone()["count"]
                == 0
            )


def test_policy_change_closes_every_open_web_segment_and_break(tmp_path, postgres_url):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app):
        database = app.state.database
        owner = database.get_user_by_email("admin@example.test")
        member = _member(database)
        project = database.create_project("Operations", "", owner["id"])
        database.add_project_member(project["id"], member["id"])

        owner_device = database.web_timer_device(owner["id"], project["id"])
        owner_session = database.sync_work_session(
            owner_device, "active", None, project["id"], transition=True
        )
        database.start_break(owner["id"], owner_session["id"])
        database.sync_work_session(
            owner_device, "paused", None, project["id"], transition=True
        )
        member_device = database.web_timer_device(member["id"], project["id"])
        member_session = database.sync_work_session(
            member_device, "active", None, project["id"], transition=True
        )

        database.update_organization_settings({"allowed_apps": "desktop_only"})

        assert database.active_timer(owner["id"]) is None
        assert database.active_timer(member["id"]) is None
        assert database.get_work_session(owner_session["id"])["status"] == "stopped"
        assert database.get_work_session(member_session["id"])["status"] == "stopped"
        with database.connect() as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) count FROM work_session_segments WHERE ended_at IS NULL"
                ).fetchone()["count"]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) count FROM work_breaks WHERE ended_at IS NULL"
                ).fetchone()["count"]
                == 0
            )


def test_member_all_apps_override_and_subsequent_restriction_are_immediate(
    tmp_path, postgres_url
):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        owner = database.get_user_by_email("admin@example.test")
        member = _member(database)
        project = database.create_project("Assigned", "", owner["id"])
        database.add_project_member(project["id"], member["id"])
        database.update_organization_settings({"allowed_apps": "desktop_only"})
        database.update_member_tracking_settings(
            member["id"], _member_policy("all"), owner["id"]
        )
        _login(client, member["email"], "member password long enough")

        dashboard = client.get("/")
        assert "Start tracking from this browser" in dashboard.text
        started = client.post(
            "/timer/start",
            data={"project_id": project["id"], "csrf": _csrf(dashboard)},
            follow_redirects=False,
        )
        assert started.status_code == 303
        assert database.active_timer(member["id"])["status"] == "active"

        # Reapplying/changing the organization default must evaluate the effective
        # member policy rather than stopping every virtual-device session blindly.
        database.update_organization_settings({"allowed_apps": "desktop_only"})
        assert database.active_timer(member["id"])["status"] == "active"

        database.update_member_tracking_settings(
            member["id"], _member_policy("desktop_only"), owner["id"]
        )
        assert database.active_timer(member["id"]) is None
        assert "Desktop tracking required" in client.get("/").text

        web_device = database.web_timer_device(member["id"], project["id"])
        with pytest.raises(ValueError, match="Desktop tracking is required"):
            database.sync_work_session(
                web_device, "active", None, project["id"], transition=True
            )


def test_allowed_apps_values_are_validated(tmp_path, postgres_url):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app):
        database = app.state.database
        owner = database.get_user_by_email("admin@example.test")
        member = _member(database)
        with pytest.raises(ValueError, match="all or desktop only"):
            database.update_organization_settings({"allowed_apps": "mobile_only"})
        with pytest.raises(ValueError, match="all, desktop only, or inherited"):
            database.update_member_tracking_settings(
                member["id"], _member_policy("mobile_only"), owner["id"]
            )

import base64
import html
import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient

from api.config import Settings
from api.database import Database
from api.main import create_app
from api.security import hash_password


def _csrf(response) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert match, response.text
    return match.group(1)


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


def _login(client, email: str, password: str) -> None:
    login_page = client.get("/login")
    login = client.post(
        "/login",
        data={"email": email, "password": password, "csrf": _csrf(login_page)},
        follow_redirects=False,
    )
    assert login.status_code == 303


def _project_with_session(database: Database, owner: dict):
    project = database.create_project("Alpha", "", owner["id"])
    database.add_project_member(project["id"], owner["id"])
    task = database.create_task(project["id"], "Review AI output", "", owner["id"])
    _, token = database.create_device("Laptop", owner["id"], project["id"])
    device = database.authenticate_device(token)
    database.sync_work_session(device, "active", task["id"])
    return project, task


def test_list_work_sessions_returns_joined_project_and_task_names(database: Database):
    admin = database.bootstrap_admin("admin@example.com", "hash")
    project, task = _project_with_session(database, admin)

    sessions = database.list_work_sessions(None, project["id"])

    assert len(sessions) == 1
    assert sessions[0]["project_name"] == project["name"]
    assert sessions[0]["task_name"] == task["name"]
    assert sessions[0]["tracked_seconds"] >= 0


def test_project_page_renders_work_sessions_for_admin(tmp_path, postgres_url):
    settings = _settings(tmp_path, postgres_url)
    app = create_app(settings)
    with TestClient(app) as client:
        database = app.state.database
        admin = database.get_user_by_email(settings.admin_email)
        project, task = _project_with_session(database, admin)

        _login(client, settings.admin_email, settings.admin_password)
        page = client.get(f"/projects/{project['id']}")

        assert page.status_code == 200
        assert project["name"] in page.text
        assert task["name"] in page.text


def test_project_page_renders_work_sessions_for_member(tmp_path, postgres_url):
    settings = _settings(tmp_path, postgres_url)
    app = create_app(settings)
    with TestClient(app) as client:
        database = app.state.database
        admin = database.get_user_by_email(settings.admin_email)
        _, invite_token = database.create_invitation(
            "person@example.test", admin["id"], 24
        )
        member = database.accept_invitation(
            invite_token, hash_password("member password long enough")
        )
        project, task = _project_with_session(database, member)

        _login(client, "person@example.test", "member password long enough")
        page = client.get(f"/projects/{project['id']}")

        assert page.status_code == 200
        assert task["name"] in page.text
        assert 'action="/devices"' in page.text
        assert "Create enrollment" in page.text
        assert 'name="tracker_kind"' in page.text

        enrolled = client.post(
            "/devices",
            data={
                "name": "Developer laptop",
                "project_id": project["id"],
                "csrf": _csrf(page),
            },
        )
        assert enrolled.status_code == 200
        assert 'server_url = "http://127.0.0.1:8000"' in html.unescape(enrolled.text)
        assert "consent_confirmed = true" in enrolled.text
        assert f'project_id = "{project["id"]}"' in html.unescape(enrolled.text)
        config_text = html.unescape(enrolled.text)
        bridge_token = re.search(r'website_bridge_token = "([^"]+)"', config_text)
        assert bridge_token and len(bridge_token.group(1)) >= 32
        assert "website_bridge_port = 8765" in config_text
        assert "Download agent.toml" in enrolled.text
        assert "Connect the iOS or Android tracker" not in enrolled.text
        assert 'id="mobileEnrollment"' not in enrolled.text


def test_mobile_enrollment_is_native_only_and_classified(tmp_path, postgres_url):
    settings = _settings(tmp_path, postgres_url)
    app = create_app(settings)
    with TestClient(app) as client:
        database = app.state.database
        admin = database.get_user_by_email(settings.admin_email)
        project = database.create_project("Field work", "", admin["id"])
        database.add_project_member(project["id"], admin["id"])
        _login(client, settings.admin_email, settings.admin_password)
        page = client.get(f"/projects/{project['id']}")

        enrolled = client.post(
            "/devices",
            data={
                "name": "Work phone",
                "project_id": project["id"],
                "tracker_kind": "mobile",
                "csrf": _csrf(page),
            },
        )

        assert enrolled.status_code == 200
        assert "Connect the iOS or Android tracker" in enrolled.text
        assert 'id="agentConfig"' not in enrolled.text
        assert "Download agent.toml" not in enrolled.text
        mobile_match = re.search(
            r'<textarea id="mobileEnrollment"[^>]*>(.*?)</textarea>',
            enrolled.text,
            re.DOTALL,
        )
        assert mobile_match
        mobile_enrollment = json.loads(html.unescape(mobile_match.group(1)))
        assert mobile_enrollment["server_url"] == "http://127.0.0.1:8000"
        assert mobile_enrollment["project_id"] == project["id"]
        assert len(mobile_enrollment["device_token"]) >= 32
        device_id = re.search(r'href="/devices/([^"]+)"', enrolled.text)
        assert device_id
        assert database.get_device(device_id.group(1))["tracker_kind"] == "mobile"
        assert "dayfinch-mobile-enrollment.json" in enrolled.text


def test_enrollment_shows_configured_signed_agent_downloads(tmp_path, postgres_url):
    settings = replace(
        _settings(tmp_path, postgres_url),
        agent_windows_url="https://downloads.example.test/dayfinch-windows.exe",
        agent_macos_url="https://downloads.example.test/dayfinch-macos.pkg",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        database = app.state.database
        admin = database.get_user_by_email(settings.admin_email)
        project = database.create_project("Agent downloads", "", admin["id"])
        database.add_project_member(project["id"], admin["id"])
        _login(client, settings.admin_email, settings.admin_password)
        page = client.get(f"/projects/{project['id']}")

        enrolled = client.post(
            "/devices",
            data={
                "name": "Release laptop",
                "project_id": project["id"],
                "csrf": _csrf(page),
            },
        )

        assert "Download for Windows" in enrolled.text
        assert "Download for macOS" in enrolled.text
        assert "Download for Linux" not in enrolled.text
        assert settings.agent_windows_url in enrolled.text


def test_enrollment_embeds_authenticated_update_channel(tmp_path, postgres_url):
    public_key = base64.urlsafe_b64encode(b"u" * 32).rstrip(b"=").decode()
    settings = replace(
        _settings(tmp_path, postgres_url),
        agent_update_manifest_url="https://releases.example.test/dayfinch-update.json",
        agent_update_public_key=public_key,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        database = app.state.database
        admin = database.get_user_by_email(settings.admin_email)
        project = database.create_project("Update channel", "", admin["id"])
        database.add_project_member(project["id"], admin["id"])
        _login(client, settings.admin_email, settings.admin_password)
        page = client.get(f"/projects/{project['id']}")

        enrolled = client.post(
            "/devices",
            data={
                "name": "Managed laptop",
                "project_id": project["id"],
                "csrf": _csrf(page),
            },
        )
        configuration = html.unescape(enrolled.text)

        assert (
            f'update_manifest_url = "{settings.agent_update_manifest_url}"'
            in configuration
        )
        assert f'update_public_key = "{public_key}"' in configuration
        assert 'update_mode = "notify"' in configuration


def test_project_viewer_is_read_only_and_cannot_enroll_or_track(tmp_path, postgres_url):
    settings = _settings(tmp_path, postgres_url)
    app = create_app(settings)
    with TestClient(app) as client:
        database = app.state.database
        admin = database.get_user_by_email(settings.admin_email)
        _, invite_token = database.create_invitation(
            "viewer@example.test", admin["id"], 24
        )
        viewer = database.accept_invitation(
            invite_token, hash_password("viewer password long enough")
        )
        project = database.create_project("Visible project", "", admin["id"])
        database.add_project_member(project["id"], viewer["id"])
        with database.connect() as connection:
            connection.execute(
                "UPDATE users SET role='viewer' WHERE id=%s", (viewer["id"],)
            )

        _login(client, "viewer@example.test", "viewer password long enough")
        page = client.get(f"/projects/{project['id']}")
        assert page.status_code == 200
        assert "Create enrollment" not in page.text
        assert 'name="tracker_kind"' not in page.text
        csrf = _csrf(page)

        assert (
            client.post(
                "/devices",
                data={
                    "name": "Forbidden laptop",
                    "project_id": project["id"],
                    "csrf": csrf,
                },
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/timer/start",
                data={"project_id": project["id"], "csrf": csrf},
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/projects/{project['id']}/todos",
                data={"name": "Forbidden", "description": "", "csrf": csrf},
            ).status_code
            == 403
        )


def test_project_viewer_activity_is_limited_to_assigned_projects(
    tmp_path, postgres_url
):
    settings = _settings(tmp_path, postgres_url)
    app = create_app(settings)
    with TestClient(app) as client:
        database = app.state.database
        admin = database.get_user_by_email(settings.admin_email)
        _, invite_token = database.create_invitation(
            "viewer@example.test", admin["id"], 24
        )
        viewer = database.accept_invitation(
            invite_token, hash_password("viewer password long enough")
        )
        visible = database.create_project("Visible project", "", admin["id"])
        hidden = database.create_project("Hidden project", "", admin["id"])
        database.add_project_member(visible["id"], viewer["id"])
        with database.connect() as connection:
            connection.execute(
                "UPDATE users SET role='viewer' WHERE id=%s", (viewer["id"],)
            )
        for project, label in ((visible, "Visible app"), (hidden, "Hidden app")):
            device, _ = database.create_device(
                f"{label} laptop", admin["id"], project["id"]
            )
            database.add_record(
                {
                    "id": str(uuid4()),
                    "device_id": device["id"],
                    "captured_at": datetime.now(UTC).isoformat(),
                    "keyboard_events": 4,
                    "mouse_clicks": 2,
                    "mouse_distance": 20,
                    "active_app": label,
                    "agent_version": "test",
                    "screenshot_path": f"{label}.jpg",
                    "focused_seconds": 300,
                    "interactive_seconds": 180,
                    "user_id": admin["id"],
                    "project_id": project["id"],
                    "activity_percent": 60,
                }
            )

        _login(client, "viewer@example.test", "viewer password long enough")
        dashboard = client.get("/")
        screenshots = client.get("/activity")
        apps = client.get("/activity?tab=apps")

        assert dashboard.status_code == 200
        assert "Visible project" in dashboard.text
        assert "Hidden project" not in dashboard.text
        assert "Approval inbox" not in dashboard.text
        assert screenshots.status_code == 200
        assert "Visible project" in screenshots.text
        assert "Hidden project" not in screenshots.text
        assert "Visible app" in apps.text
        assert "Hidden app" not in apps.text


def test_project_manager_and_project_viewer_permissions(tmp_path, postgres_url):
    settings = _settings(tmp_path, postgres_url)
    app = create_app(settings)
    with TestClient(app) as client:
        database = app.state.database
        admin = database.get_user_by_email(settings.admin_email)

        def member(email: str):
            _, token = database.create_invitation(email, admin["id"], 24)
            return database.accept_invitation(
                token, hash_password("member password long enough")
            )

        project_manager = member("manager@example.test")
        coworker = member("coworker@example.test")
        project_viewer = member("readonly@example.test")
        project = database.create_project("Scoped project", "", admin["id"])
        database.add_project_member(project["id"], project_manager["id"], "manager")
        database.add_project_member(project["id"], coworker["id"], "worker")
        _, device_token = database.create_device(
            "Coworker laptop", coworker["id"], project["id"]
        )
        device = database.authenticate_device(device_token)
        database.sync_work_session(device, "active", None, project["id"])
        database.add_record(
            {
                "id": str(uuid4()),
                "device_id": device["id"],
                "captured_at": datetime.now(UTC).isoformat(),
                "keyboard_events": 5,
                "mouse_clicks": 3,
                "mouse_distance": 30,
                "active_app": "Coworker editor",
                "agent_version": "test",
                "screenshot_path": "scoped.jpg",
                "focused_seconds": 300,
                "interactive_seconds": 200,
                "user_id": coworker["id"],
                "project_id": project["id"],
                "activity_percent": 66,
            }
        )

        _login(client, project_manager["email"], "member password long enough")
        manager_page = client.get(f"/projects/{project['id']}")
        csrf = _csrf(manager_page)
        assert "Coworker laptop" in manager_page.text
        assert 'action="/projects/' in manager_page.text
        assert "Project role for" in manager_page.text

        task = client.post(
            f"/projects/{project['id']}/tasks",
            data={
                "name": "Managed task",
                "description": "",
                "billable": 1,
                "csrf": csrf,
            },
            follow_redirects=False,
        )
        assert task.status_code == 303
        assigned = client.post(
            f"/projects/{project['id']}/members",
            data={
                "user_id": project_viewer["id"],
                "project_role": "viewer",
                "csrf": csrf,
            },
            follow_redirects=False,
        )
        assert assigned.status_code == 303
        finance = client.post(
            f"/projects/{project['id']}/settings",
            data={
                "color": "#000000",
                "budget_type": "cost",
                "budget_amount": "123",
                "budget_minutes": 0,
                "billable_rate": "0",
                "csrf": csrf,
            },
        )
        assert finance.status_code == 200
        updated_project = database.get_project(project["id"])
        assert updated_project["budget_type"] == "cost"
        assert updated_project["budget_amount"] == Decimal("123")

        client.cookies.clear()
        _login(client, project_viewer["email"], "member password long enough")
        viewer_page = client.get(f"/projects/{project['id']}")
        viewer_csrf = _csrf(viewer_page)
        activity = client.get("/activity")
        assert "Coworker laptop" in viewer_page.text
        assert "Managed task" in viewer_page.text
        assert "Create enrollment" not in viewer_page.text
        assert 'name="tracker_kind"' not in viewer_page.text
        assert "Create task" not in viewer_page.text
        assert "Coworker editor" in activity.text
        assert (
            client.post(
                "/timer/start",
                data={"project_id": project["id"], "csrf": viewer_csrf},
            ).status_code
            == 403
        )

import re

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

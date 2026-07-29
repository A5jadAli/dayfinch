import re

from fastapi.testclient import TestClient

from api.config import Settings
from api.main import create_app


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


def _actions(database) -> dict[str, dict]:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT action, actor_user_id, target_type, details FROM audit_events"
        ).fetchall()
    return {row["action"]: dict(row) for row in rows}


def test_account_and_enrollment_actions_are_audited(tmp_path, postgres_url):
    """Actions that grant access must leave a trace, not just screenshot views."""
    settings = _settings(tmp_path, postgres_url)
    app = create_app(settings)
    with TestClient(app) as client:
        database = app.state.database
        login_page = client.get("/login")
        client.post(
            "/login",
            data={
                "email": settings.admin_email,
                "password": settings.admin_password,
                "csrf": _csrf(login_page),
            },
            follow_redirects=False,
        )
        dashboard = client.get("/")
        admin = database.get_user_by_email(settings.admin_email)

        invited = client.post(
            "/invitations",
            data={"email": "newbie@example.test", "csrf": _csrf(dashboard)},
        )
        assert invited.status_code == 200
        raw_token = re.search(r"/invite/([A-Za-z0-9_\-]+)", invited.text).group(1)

        created = client.post(
            "/projects",
            data={"name": "Alpha", "description": "d", "csrf": _csrf(dashboard)},
            follow_redirects=False,
        )
        project_id = created.headers["location"].rsplit("/", 1)[-1]

        invite_page = client.get(f"/invite/{raw_token}")
        accepted = client.post(
            f"/invite/{raw_token}",
            data={
                "password": "member password long enough",
                "password_confirm": "member password long enough",
                "csrf": _csrf(invite_page),
            },
            follow_redirects=False,
        )
        assert accepted.status_code == 303

        member = database.get_user_by_email("newbie@example.test")
        database.add_project_member(project_id, member["id"])
        member_dashboard = client.get("/")
        enrolled = client.post(
            "/devices",
            data={
                "name": "Member laptop",
                "project_id": project_id,
                "csrf": _csrf(member_dashboard),
            },
        )
        assert enrolled.status_code == 200

        actions = _actions(database)
        assert actions["invitation.created"]["actor_user_id"] == admin["id"]
        assert actions["invitation.created"]["details"] == "newbie@example.test"
        assert actions["invitation.accepted"]["actor_user_id"] == member["id"]
        assert actions["invitation.accepted"]["target_type"] == "user"
        assert actions["device.enrolled"]["actor_user_id"] == member["id"]
        assert actions["device.enrolled"]["details"] == "Member laptop"


def test_login_success_failure_and_logout_are_audited(tmp_path, postgres_url):
    settings = _settings(tmp_path, postgres_url)
    app = create_app(settings)
    with TestClient(app) as client:
        database = app.state.database

        bad = client.post(
            "/login",
            data={
                "email": settings.admin_email,
                "password": "wrong password entirely",
                "csrf": _csrf(client.get("/login")),
            },
            follow_redirects=False,
        )
        assert bad.status_code == 401

        good = client.post(
            "/login",
            data={
                "email": settings.admin_email,
                "password": settings.admin_password,
                "csrf": _csrf(client.get("/login")),
            },
            follow_redirects=False,
        )
        assert good.status_code == 303

        client.post(
            "/logout",
            data={"csrf": _csrf(client.get("/"))},
            follow_redirects=False,
        )

        actions = _actions(database)
        assert actions["auth.login_failed"]["details"] == settings.admin_email
        assert "auth.login" in actions
        assert "auth.logout" in actions


def test_failed_invitation_is_not_audited(tmp_path, postgres_url):
    settings = _settings(tmp_path, postgres_url)
    app = create_app(settings)
    with TestClient(app) as client:
        database = app.state.database
        login_page = client.get("/login")
        client.post(
            "/login",
            data={
                "email": settings.admin_email,
                "password": settings.admin_password,
                "csrf": _csrf(login_page),
            },
            follow_redirects=False,
        )
        dashboard = client.get("/")
        rejected = client.post(
            "/invitations",
            data={"email": settings.admin_email, "csrf": _csrf(dashboard)},
        )

        assert rejected.status_code == 400
        assert "invitation.created" not in _actions(database)

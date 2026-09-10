import re
from dataclasses import replace
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from api.config import Settings
from api.main import create_app
from api.security import hash_password, verify_password


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


def test_invitation_is_one_time_and_creates_member(tmp_path, postgres_url):
    settings = _settings(tmp_path, postgres_url)
    app = create_app(settings)
    with TestClient(app):
        database = app.state.database
        admin = database.get_user_by_email(settings.admin_email)
        invitation, token = database.create_invitation(
            "person@example.test", admin["id"], 24
        )
        assert invitation["email"] == "person@example.test"
        assert database.get_invitation(token)["email"] == "person@example.test"

        password_hash = hash_password("member password long enough")
        member = database.accept_invitation(token, password_hash)
        assert member["role"] == "member"
        assert member["enabled"] == 1
        assert verify_password(
            "member password long enough",
            database.get_user_by_email("person@example.test")["password_hash"],
        )
        assert database.get_invitation(token) is None
        assert database.accept_invitation(token, password_hash) is None


def test_member_can_delete_own_interval_but_cannot_invite(tmp_path, postgres_url):
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
        project = database.create_project("Member project", "", admin["id"])
        database.add_project_member(project["id"], member["id"])
        device, device_token = database.create_device(
            "Member laptop", member["id"], project["id"]
        )
        database.sync_work_session(
            device,
            "active",
            None,
            project["id"],
            observed_at=datetime(2026, 7, 27, 10, 0, tzinfo=UTC),
        )
        other_device, _ = database.create_device(
            "Someone else's laptop", admin["id"], project["id"]
        )

        payload = {
            "record_id": "7e814da1-abf7-46e6-b116-30facaa96993",
            "captured_at": "2026-07-27T10:10:00+00:00",
            "keyboard_events": "9",
            "mouse_clicks": "2",
            "mouse_distance": "600",
            "active_app": "Editor",
            "agent_version": "0.2.0",
        }
        created = client.post(
            "/api/v1/activity",
            headers={"Authorization": f"Bearer {device_token}"},
            data=payload,
            files={
                "screenshot_file": ("capture.jpg", b"\xff\xd8\xfffake", "image/jpeg")
            },
        )
        assert created.status_code == 201

        login_page = client.get("/login")
        login = client.post(
            "/login",
            data={
                "email": "person@example.test",
                "password": "member password long enough",
                "csrf": _csrf(login_page),
            },
            follow_redirects=False,
        )
        assert login.status_code == 303
        screenshot = client.get(f"/screenshots/{payload['record_id']}")
        assert screenshot.status_code == 200

        device_page = client.get(f"/devices/{device['id']}")
        csrf = _csrf(device_page)
        assert client.get(f"/devices/{other_device['id']}").status_code == 404
        forbidden_invite = client.post(
            "/invitations",
            data={"email": "other@example.test", "csrf": csrf},
        )
        assert forbidden_invite.status_code == 403

        deleted = client.post(
            f"/screenshots/{payload['record_id']}/delete",
            data={"csrf": csrf},
            follow_redirects=False,
        )
        assert deleted.status_code == 303
        assert database.get_record(payload["record_id"]) is None
        assert client.get(f"/screenshots/{payload['record_id']}").status_code == 404


def test_admin_can_create_invitation_link(tmp_path, postgres_url):
    settings = _settings(tmp_path, postgres_url)
    app = create_app(settings)
    with TestClient(app) as client:
        login_page = client.get("/login")
        login = client.post(
            "/login",
            data={
                "email": settings.admin_email,
                "password": settings.admin_password,
                "csrf": _csrf(login_page),
            },
            follow_redirects=False,
        )
        assert login.status_code == 303
        dashboard = client.get("/")
        response = client.post(
            "/invitations",
            data={"email": "new@example.test", "csrf": _csrf(dashboard)},
        )
        assert response.status_code == 200
        assert "new@example.test" in response.text
        assert "/invite/" in response.text
        assert "SMTP is not configured" in response.text
        assert "http://127.0.0.1:8000/invite/" in response.text


def test_repeated_login_failures_are_throttled_across_requests(tmp_path, postgres_url):
    settings = replace(
        _settings(tmp_path, postgres_url),
        login_identity_failure_limit=3,
        login_source_failure_limit=50,
        login_window_minutes=15,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        for _ in range(3):
            failed = client.post(
                "/login",
                data={
                    "email": settings.admin_email,
                    "password": "wrong password entirely",
                    "csrf": _csrf(client.get("/login")),
                },
            )
            assert failed.status_code == 401

        blocked = client.post(
            "/login",
            data={
                "email": settings.admin_email,
                "password": settings.admin_password,
                "csrf": _csrf(client.get("/login")),
            },
        )
        assert blocked.status_code == 429
        assert blocked.headers["retry-after"] == "900"
        assert "Too many sign-in attempts" in blocked.text


def test_successful_login_clears_identity_failures(tmp_path, postgres_url):
    settings = _settings(tmp_path, postgres_url)
    app = create_app(settings)
    with TestClient(app) as client:
        failed = client.post(
            "/login",
            data={
                "email": settings.admin_email,
                "password": "wrong password entirely",
                "csrf": _csrf(client.get("/login")),
            },
        )
        assert failed.status_code == 401

        succeeded = client.post(
            "/login",
            data={
                "email": settings.admin_email,
                "password": settings.admin_password,
                "csrf": _csrf(client.get("/login")),
            },
            follow_redirects=False,
        )
        assert succeeded.status_code == 303
        with app.state.database.connect() as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) count FROM login_attempts"
                ).fetchone()["count"]
                == 0
            )


def test_two_factor_codes_are_throttled_after_password_authentication(
    tmp_path, postgres_url, monkeypatch
):
    settings = replace(
        _settings(tmp_path, postgres_url),
        login_identity_failure_limit=3,
        login_source_failure_limit=50,
    )
    app = create_app(settings)
    monkeypatch.setattr("api.routers.auth.verify_totp", lambda *_args: False)
    with TestClient(app) as client:
        admin = app.state.database.get_user_by_email(settings.admin_email)
        app.state.database.set_two_factor_secret(
            admin["id"], "JBSWY3DPEHPK3PXP", enabled=True
        )
        login = client.post(
            "/login",
            data={
                "email": settings.admin_email,
                "password": settings.admin_password,
                "csrf": _csrf(client.get("/login")),
            },
            follow_redirects=False,
        )
        assert login.headers["location"] == "/two-factor"

        for _ in range(3):
            page = client.get("/two-factor")
            failed = client.post(
                "/two-factor", data={"code": "000000", "csrf": _csrf(page)}
            )
            assert failed.status_code == 401

        page = client.get("/two-factor")
        blocked = client.post(
            "/two-factor", data={"code": "000000", "csrf": _csrf(page)}
        )
        assert blocked.status_code == 429
        assert blocked.headers["retry-after"] == "900"
        assert "Too many authentication-code attempts" in blocked.text


def test_metadata_only_provider_integrations_are_not_exposed(tmp_path, postgres_url):
    settings = _settings(tmp_path, postgres_url)
    app = create_app(settings)
    with TestClient(app) as client:
        login_page = client.get("/login")
        login = client.post(
            "/login",
            data={
                "email": settings.admin_email,
                "password": settings.admin_password,
                "csrf": _csrf(login_page),
            },
            follow_redirects=False,
        )
        assert login.status_code == 303
        settings_page = client.get("/settings")
        assert "Jira" not in settings_page.text
        assert "QuickBooks" not in settings_page.text
        assert (
            client.post(
                "/integrations",
                data={
                    "provider": "Jira",
                    "display_name": "Fake connection",
                    "csrf": _csrf(settings_page),
                },
            ).status_code
            == 404
        )

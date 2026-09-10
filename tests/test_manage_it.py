import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from api.config import Settings
from api.main import create_app
from api.security import hash_password


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


def test_manage_it_can_revoke_devices_without_activity_access(tmp_path, postgres_url):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        admin = database.get_user_by_email("admin@example.test")
        _, invitation_token = database.create_invitation(
            "it@example.test", admin["id"], 24
        )
        it_user = database.accept_invitation(
            invitation_token, hash_password("member password long enough")
        )
        database.set_user_profile(
            it_user["id"],
            "member",
            "IT operator",
            Decimal("0"),
            Decimal("0"),
            0,
            0,
            True,
        )
        project = database.create_project("Private project", "", admin["id"])
        database.add_project_member(project["id"], admin["id"])
        device, device_token = database.create_device(
            "Admin laptop", admin["id"], project["id"]
        )
        database.sync_work_session(
            device,
            "active",
            None,
            project["id"],
            observed_at=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
        )
        record_id = "e4667298-e4db-4239-b731-fe63211da9b9"
        uploaded = client.post(
            "/api/v1/activity",
            headers={"Authorization": f"Bearer {device_token}"},
            data={
                "record_id": record_id,
                "captured_at": "2026-09-08T12:00:00+00:00",
                "keyboard_events": "12",
                "mouse_clicks": "3",
                "mouse_distance": "900",
                "active_app": "Confidential editor",
                "agent_version": "0.6.0",
            },
            files={
                "screenshot_file": ("capture.jpg", b"\xff\xd8\xffprivate", "image/jpeg")
            },
        )
        assert uploaded.status_code == 201

        _login(client, "it@example.test", "member password long enough")
        it_page = client.get("/it-management")
        assert it_page.status_code == 200
        assert "SSO & SCIM" in it_page.text
        assert client.get("/settings").status_code == 403
        assert client.get("/people").status_code == 403
        updated = client.post(
            "/it-management",
            data={
                "csrf": _csrf(it_page),
                "screenshot_frequency": 3,
                "idle_timeout_minutes": 15,
                "screenshot_blur": "on",
                "name": "Must not be changed",
                "pay_period": "monthly",
            },
            follow_redirects=False,
        )
        assert updated.status_code == 303
        policy = database.organization_settings()
        assert policy["screenshot_frequency"] == 3
        assert policy["idle_timeout_minutes"] == 15
        assert policy["screenshot_blur"] is True
        assert policy["name"] == "Dayfinch Workspace"
        assert policy["pay_period"] == "weekly"

        inventory = client.get("/devices")
        assert inventory.status_code == 200
        assert "Device management" in inventory.text
        assert "Admin laptop" in inventory.text

        detail = client.get(f"/devices/{device['id']}")
        assert detail.status_code == 200
        assert "Screenshot and activity contents remain private" in detail.text
        assert "Confidential editor" not in detail.text
        assert f"/screenshots/{record_id}" not in detail.text
        assert client.get(f"/screenshots/{record_id}").status_code == 404

        revoked = client.post(
            f"/devices/{device['id']}/enabled",
            data={"enabled": 0, "csrf": _csrf(detail)},
            follow_redirects=False,
        )
        assert revoked.status_code == 303
        assert database.authenticate_device(device_token) is None


def test_standard_member_cannot_open_device_inventory(tmp_path, postgres_url):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        admin = database.get_user_by_email("admin@example.test")
        _, invitation_token = database.create_invitation(
            "member@example.test", admin["id"], 24
        )
        database.accept_invitation(
            invitation_token, hash_password("member password long enough")
        )

        _login(client, "member@example.test", "member password long enough")
        dashboard = client.get("/")
        assert "Device management" not in dashboard.text
        assert "IT management" not in dashboard.text
        assert client.get("/devices").status_code == 403
        assert client.get("/it-management").status_code == 403


def test_organization_manager_cannot_assign_owner_or_manage_it(tmp_path, postgres_url):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        owner = database.get_user_by_email("admin@example.test")
        _, manager_token = database.create_invitation(
            "manager@example.test", owner["id"], 24
        )
        manager = database.accept_invitation(
            manager_token, hash_password("manager password long enough")
        )
        database.set_user_profile(
            manager["id"],
            "manager",
            "Manager",
            Decimal("0"),
            Decimal("0"),
            0,
        )
        _, member_token = database.create_invitation(
            "worker@example.test", owner["id"], 24
        )
        member = database.accept_invitation(
            member_token, hash_password("worker password long enough")
        )
        project = database.create_project("Payroll authorization", "", owner["id"])
        database.add_project_member(project["id"], member["id"])
        database.set_user_profile(
            member["id"],
            "member",
            "Worker",
            Decimal("20"),
            Decimal("40"),
            0,
        )
        started = datetime(2026, 9, 1, 9, tzinfo=UTC)
        entry = database.add_manual_time(
            member["id"],
            project["id"],
            None,
            started,
            started + timedelta(hours=1),
            "Approved work",
        )
        database.review_item("manual_time_entries", entry, owner["id"], "approved")
        sheet = database.submit_timesheet(
            member["id"], date(2026, 9, 1), date(2026, 9, 7)
        )
        database.review_timesheet(sheet["id"], owner["id"], "approved", "")
        payment_id = database.create_payroll(
            member["id"], date(2026, 9, 1), date(2026, 9, 7), "USD"
        )

        _login(client, "manager@example.test", "manager password long enough")
        people = client.get("/people")
        common = {
            "csrf": _csrf(people),
            "full_name": "Worker",
            "pay_rate": "0",
            "bill_rate": "0",
            "weekly_limit_minutes": "0",
            "daily_limit_minutes": "0",
        }
        promote = client.post(
            f"/people/{member['id']}",
            data={**common, "role": "admin", "manage_it": "on"},
        )
        assert promote.status_code == 403
        unchanged = database.get_user(member["id"])
        assert unchanged["role"] == "member"
        assert unchanged["manage_it"] is False

        normal_edit = client.post(
            f"/people/{member['id']}",
            data={**common, "role": "member", "manage_it": "on"},
            follow_redirects=False,
        )
        assert normal_edit.status_code == 303
        unchanged = database.get_user(member["id"])
        assert unchanged["manage_it"] is False

        owner_edit = client.post(
            f"/people/{owner['id']}", data={**common, "role": "manager"}
        )
        assert owner_edit.status_code == 403
        assert (
            client.post(
                f"/people/{owner['id']}/status",
                data={"csrf": _csrf(people), "enabled": "false"},
            ).status_code
            == 403
        )

        financials = client.get("/financials")
        assert financials.status_code == 200
        assert "Payroll paid" not in financials.text
        assert "Generate payroll" not in financials.text
        assert "Payroll runs" not in financials.text
        assert (
            client.post(
                "/payroll",
                data={
                    "csrf": _csrf(financials),
                    "user_id": member["id"],
                    "period_start": "2026-09-08",
                    "period_end": "2026-09-14",
                },
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/financials/payroll/{payment_id}/status",
                data={"csrf": _csrf(financials), "item_status": "paid"},
            ).status_code
            == 403
        )

        settings = client.get("/settings")
        assert settings.status_code == 200
        assert "Organization name" not in settings.text
        assert "Screenshot storage" not in settings.text
        policy_update = client.post(
            "/settings",
            data={
                "csrf": _csrf(settings),
                "screenshot_frequency": "1",
                "idle_timeout_minutes": "30",
                "name": "Manager takeover",
                "pay_period": "monthly",
                "retention_days": "1",
            },
            follow_redirects=False,
        )
        assert policy_update.status_code == 303
        policy = database.organization_settings()
        assert policy["name"] == "Dayfinch Workspace"
        assert policy["pay_period"] == "weekly"
        assert policy["retention_days"] == 90


def test_owner_role_and_team_invariants_are_enforced(tmp_path, postgres_url):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        owner = database.get_user_by_email("admin@example.test")
        _, token = database.create_invitation("worker@example.test", owner["id"], 24)
        worker = database.accept_invitation(
            token, hash_password("worker password long enough")
        )
        _login(client, "admin@example.test", "correct horse battery staple")
        people = client.get("/people")
        profile = {
            "csrf": _csrf(people),
            "role": "admin",
            "full_name": "Worker",
            "pay_rate": "0",
            "bill_rate": "0",
            "weekly_limit_minutes": "0",
            "daily_limit_minutes": "0",
        }
        assert client.post(f"/people/{worker['id']}", data=profile).status_code == 403
        assert (
            client.post(
                f"/people/{owner['id']}/status",
                data={"csrf": _csrf(people), "enabled": "false"},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/people/00000000-0000-0000-0000-000000000000/status",
                data={"csrf": _csrf(people), "enabled": "false"},
            ).status_code
            == 404
        )
        created = client.post(
            "/teams",
            data={"csrf": _csrf(people), "name": "Engineering"},
            follow_redirects=False,
        )
        assert created.status_code == 303
        duplicate = client.post(
            "/teams", data={"csrf": _csrf(people), "name": "Engineering"}
        )
        assert duplicate.status_code == 409
        team_id = database.list_teams()[0]["id"]
        assert (
            client.post(
                f"/teams/{team_id}/leads",
                data={"csrf": _csrf(people), "user_id": owner["id"]},
            ).status_code
            == 422
        )

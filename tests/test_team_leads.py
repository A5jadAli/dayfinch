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


def _member(database, owner: dict, email: str, password: str) -> dict:
    _, token = database.create_invitation(email, owner["id"], 24)
    return database.accept_invitation(token, hash_password(password))


def test_team_lead_permissions_are_scoped_and_never_grant_activity_access(
    tmp_path, postgres_url
):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        owner = database.get_user_by_email("admin@example.test")
        lead = _member(
            database, owner, "lead@example.test", "lead password long enough"
        )
        worker = _member(
            database, owner, "worker@example.test", "worker password long enough"
        )
        outsider = _member(
            database, owner, "outsider@example.test", "outsider password long enough"
        )
        project = database.create_project("Secret project", "", owner["id"])
        database.add_project_member(project["id"], worker["id"])
        database.add_project_member(project["id"], outsider["id"])
        team_id = database.create_team("Engineering", lead["id"])
        other_team_id = database.create_team("Operations", None)
        database.add_team_member(team_id, worker["id"])
        database.add_team_member(team_id, owner["id"])
        database.set_team_lead_permissions(
            team_id,
            lead["id"],
            {
                "approve_timesheets": True,
                "approve_manual_time": True,
                "approve_time_off": True,
                "manage_schedules": True,
                "manage_projects": True,
                "manage_members": True,
                "manage_financials": True,
            },
        )
        database.add_team_project(team_id, project["id"])
        team = database.list_teams()[0]
        assert team["member_count"] == 3
        assert team["leads"][0]["email"] == "lead@example.test"
        assert team["leads"][0]["approve_timesheets"] is True
        assert team["projects"] == [{"id": project["id"], "name": "Secret project"}]
        assert not database.team_lead_can_manage_user(
            lead["id"], owner["id"], "manage_members"
        )

        start = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
        worker_time = database.add_manual_time(
            worker["id"],
            project["id"],
            None,
            start,
            start + timedelta(hours=1),
            "worker-team-entry",
        )
        outsider_time = database.add_manual_time(
            outsider["id"],
            project["id"],
            None,
            start,
            start + timedelta(hours=1),
            "outsider-private-entry",
        )
        lead_time = database.add_manual_time(
            lead["id"],
            project["id"],
            None,
            start,
            start + timedelta(hours=1),
            "lead-own-entry",
        )
        worker_leave = database.add_time_off(
            worker["id"],
            "paid",
            date(2026, 9, 9),
            date(2026, 9, 9),
            480,
            "worker leave",
        )
        outsider_leave = database.add_time_off(
            outsider["id"],
            "paid",
            date(2026, 9, 9),
            date(2026, 9, 9),
            480,
            "outsider leave",
        )
        worker_expense = database.add_expense(
            worker["id"],
            project["id"],
            date(2026, 9, 8),
            "Software",
            Decimal("10"),
            "USD",
            "worker expense",
        )
        outsider_expense = database.add_expense(
            outsider["id"],
            project["id"],
            date(2026, 9, 8),
            "Software",
            Decimal("20"),
            "USD",
            "outsider expense",
        )
        device, device_token = database.create_device(
            "Worker laptop", worker["id"], project["id"]
        )
        database.sync_work_session(
            device,
            "active",
            None,
            project["id"],
            observed_at=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
        )
        record_id = "d216e380-1536-4e4c-92e8-f09663be172a"
        assert (
            client.post(
                "/api/v1/activity",
                headers={"Authorization": f"Bearer {device_token}"},
                data={
                    "record_id": record_id,
                    "captured_at": "2026-09-08T12:00:00+00:00",
                    "keyboard_events": "3",
                    "mouse_clicks": "1",
                    "mouse_distance": "20",
                    "active_app": "Private editor",
                    "agent_version": "0.6.0",
                },
                files={
                    "screenshot_file": (
                        "capture.jpg",
                        b"\xff\xd8\xffprivate",
                        "image/jpeg",
                    )
                },
            ).status_code
            == 201
        )
        database.sync_work_session(
            device,
            "stopped",
            None,
            project["id"],
            observed_at=datetime(2026, 9, 8, 12, 1, tzinfo=UTC),
        )

        _login(client, "lead@example.test", "lead password long enough")
        assert client.get("/people").status_code == 403
        dashboard = client.get("/")
        assert "Create project" in dashboard.text
        assert "Create invitation" not in dashboard.text
        assert (
            client.post(
                "/projects",
                data={
                    "csrf": _csrf(dashboard),
                    "name": "Lead project",
                    "description": "Created in delegated team",
                    "team_id": team_id,
                },
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert (
            client.post(
                "/projects",
                data={
                    "csrf": _csrf(dashboard),
                    "name": "Cross-team project",
                    "team_id": other_team_id,
                },
            ).status_code
            == 403
        )
        project_page = client.get(f"/projects/{project['id']}")
        assert project_page.status_code == 200
        assert "Budget & billing" in project_page.text
        assert (
            client.post(
                f"/projects/{project['id']}/tasks",
                data={
                    "csrf": _csrf(project_page),
                    "name": "Lead-created task",
                    "billable": "1",
                },
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert (
            client.post(
                f"/projects/{project['id']}/members/{worker['id']}/role",
                data={"csrf": _csrf(project_page), "project_role": "manager"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert (
            client.post(
                f"/projects/{project['id']}/members/{outsider['id']}/role",
                data={"csrf": _csrf(project_page), "project_role": "manager"},
            ).status_code
            == 404
        )
        entries = client.get("/time-entries")
        assert entries.status_code == 200
        assert "worker-team-entry" in entries.text
        assert "outsider-private-entry" not in entries.text
        assert "Approve selected" in entries.text
        assert (
            client.post(
                f"/time-entries/{worker_time}/review",
                data={"csrf": _csrf(entries), "decision": "approved"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert (
            client.post(
                f"/time-entries/{outsider_time}/review",
                data={"csrf": _csrf(entries), "decision": "approved"},
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/time-entries/{lead_time}/review",
                data={"csrf": _csrf(entries), "decision": "approved"},
            ).status_code
            == 404
        )

        schedules = client.get("/schedules")
        assert schedules.status_code == 200
        assert "worker leave" in schedules.text
        assert "outsider leave" not in schedules.text
        assert "Publish shift" in schedules.text
        assert (
            client.post(
                f"/time-off/{worker_leave}/review",
                data={"csrf": _csrf(schedules), "decision": "approved"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert (
            client.post(
                f"/time-off/{outsider_leave}/review",
                data={"csrf": _csrf(schedules), "decision": "approved"},
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/schedules",
                data={
                    "csrf": _csrf(schedules),
                    "user_id": worker["id"],
                    "starts_at": "2026-09-10T09:00:00+00:00",
                    "ends_at": "2026-09-10T17:00:00+00:00",
                    "notes": "team shift",
                },
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert (
            client.post(
                "/schedules",
                data={
                    "csrf": _csrf(schedules),
                    "user_id": outsider["id"],
                    "starts_at": "2026-09-10T09:00:00+00:00",
                    "ends_at": "2026-09-10T17:00:00+00:00",
                },
            ).status_code
            == 404
        )

        worker_sheet = database.submit_timesheet(
            worker["id"], date(2026, 10, 1), date(2026, 10, 7)
        )
        outsider_sheet = database.submit_timesheet(
            outsider["id"], date(2026, 10, 1), date(2026, 10, 7)
        )
        timesheets = client.get("/timesheets")
        assert "worker@example.test" in timesheets.text
        assert "outsider@example.test" not in timesheets.text
        assert "Estimated" not in timesheets.text
        assert (
            client.post(
                f"/timesheets/{worker_sheet['id']}/review",
                data={"csrf": _csrf(timesheets), "decision": "approved", "note": "ok"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert (
            client.post(
                f"/timesheets/{outsider_sheet['id']}/review",
                data={"csrf": _csrf(timesheets), "decision": "approved", "note": ""},
            ).status_code
            == 404
        )

        assert client.get(f"/devices/{device['id']}").status_code == 404
        assert client.get(f"/screenshots/{record_id}").status_code == 404

        financials = client.get("/financials")
        assert "worker expense" in financials.text
        assert "outsider expense" not in financials.text
        assert "Payroll runs" not in financials.text
        assert (
            client.post(
                f"/expenses/{worker_expense}/review",
                data={"csrf": _csrf(financials), "decision": "approved"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert (
            client.post(
                f"/expenses/{outsider_expense}/review",
                data={"csrf": _csrf(financials), "decision": "approved"},
            ).status_code
            == 404
        )

        database.set_team_lead_permissions(
            team_id,
            lead["id"],
            {
                "approve_timesheets": True,
                "approve_manual_time": True,
                "manage_schedules": True,
                "approve_time_off": False,
            },
        )
        second_leave = database.add_time_off(
            worker["id"],
            "paid",
            date(2026, 9, 12),
            date(2026, 9, 12),
            480,
            "hidden leave",
        )
        schedules = client.get("/schedules")
        assert "hidden leave" not in schedules.text
        assert client.get(f"/projects/{project['id']}").status_code == 404
        assert "worker expense" not in client.get("/financials").text
        assert (
            client.post(
                f"/time-off/{second_leave}/review",
                data={"csrf": _csrf(schedules), "decision": "approved"},
            ).status_code
            == 404
        )

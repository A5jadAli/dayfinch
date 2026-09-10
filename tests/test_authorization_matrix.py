import inspect
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from api.config import Settings
from api.main import create_app
from api.security import hash_password


def _csrf(response) -> str:
    matched = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert matched, response.text
    return matched.group(1)


def _settings(tmp_path, postgres_url: str) -> Settings:
    return Settings(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024 * 1024,
        retention_days=30,
        admin_email="owner@example.test",
        database_url=postgres_url,
    )


def _member(database, owner: dict, email: str) -> dict:
    _, token = database.create_invitation(email, owner["id"], 24)
    return database.accept_invitation(
        token, hash_password("member password long enough")
    )


def _login(
    client: TestClient, email: str, password: str = "member password long enough"
) -> None:
    client.cookies.clear()
    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "email": email,
            "password": password,
            "csrf": _csrf(page),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_project_viewer_cannot_mutate_tracking_expenses_or_global_work(
    tmp_path, postgres_url
):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        owner = database.get_user_by_email("owner@example.test")
        viewer = _member(database, owner, "viewer@example.test")
        worker = _member(database, owner, "worker@example.test")
        project = database.create_project("Read-only project", "", owner["id"])
        database.add_project_member(project["id"], viewer["id"], "viewer")
        database.add_project_member(project["id"], worker["id"], "worker")

        _login(client, viewer["email"])
        dashboard = client.get("/")
        csrf = _csrf(dashboard)
        assert "Start tracking from this browser" not in dashboard.text
        assert "Add time" not in dashboard.text

        field_event = client.post(
            "/field/timer",
            headers={"X-CSRF-Token": csrf},
            json={
                "event_id": str(uuid.uuid4()),
                "observed_at": datetime.now(UTC).isoformat(),
                "action": "start",
                "project_id": project["id"],
            },
        )
        assert field_event.status_code == 403
        assert (
            client.post(
                "/time-entries",
                data={
                    "project_id": project["id"],
                    "started_at": "2026-09-08T09:00:00+00:00",
                    "ended_at": "2026-09-08T10:00:00+00:00",
                    "csrf": csrf,
                },
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/expenses",
                data={
                    "project_id": project["id"],
                    "incurred_on": "2026-09-08",
                    "category": "software",
                    "amount": "10",
                    "currency": "USD",
                    "csrf": csrf,
                },
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/projects/{project['id']}/todos",
                data={
                    "name": "Cross-project default",
                    "add_to_future_projects": "1",
                    "csrf": csrf,
                },
            ).status_code
            == 404
        )

        _login(client, worker["email"])
        project_page = client.get(f"/projects/{project['id']}")
        assert (
            client.post(
                f"/projects/{project['id']}/todos",
                data={
                    "name": "Cross-project default",
                    "add_to_future_projects": "1",
                    "csrf": _csrf(project_page),
                },
            ).status_code
            == 403
        )


def test_role_downgrade_and_project_archive_stop_active_agent_sessions(
    database,
):
    owner = database.bootstrap_admin("owner@example.test", "hash")
    worker = _member(database, owner, "worker@example.test")
    project = database.create_project("Tracked project", "", owner["id"])
    database.add_project_member(project["id"], worker["id"], "worker")
    device, token = database.create_device("Laptop", worker["id"], project["id"])
    session = database.sync_work_session(device, "active", None, project["id"])

    database.set_project_member_role(project["id"], worker["id"], "viewer")

    assert database.get_work_session(session["id"])["status"] == "stopped"
    assert database.authenticate_device(token)["project_id"] is None
    assert database.list_trackable_projects(worker["id"]) == []
    with pytest.raises(ValueError, match="cannot track"):
        database.sync_work_session(device, "active", None, project["id"])

    database.set_project_member_role(project["id"], worker["id"], "worker")
    database.sync_work_session(device, "active", None, project["id"])
    database.set_project_enabled(project["id"], False)

    sessions = database.list_work_sessions(worker["id"], project["id"])
    assert all(item["status"] == "stopped" for item in sessions)
    with pytest.raises(ValueError, match="unavailable"):
        database.sync_work_session(device, "active", None, project["id"])


def test_disabling_member_closes_time_revokes_devices_and_can_be_reenabled(
    tmp_path, postgres_url
):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        owner = database.get_user_by_email("owner@example.test")
        worker = _member(database, owner, "worker@example.test")
        project = database.create_project("Lifecycle project", "", owner["id"])
        database.add_project_member(project["id"], worker["id"])
        device, token = database.create_device("Laptop", worker["id"], project["id"])
        session = database.sync_work_session(device, "active", None, project["id"])

        _login(client, owner["email"], "correct horse battery staple")
        csrf = _csrf(client.get("/people"))
        disabled = client.post(
            f"/people/{worker['id']}/status",
            data={"enabled": "false", "csrf": csrf},
            follow_redirects=False,
        )

        assert disabled.status_code == 303
        assert database.get_work_session(session["id"])["status"] == "stopped"
        assert database.authenticate_device(token) is None
        assert database.get_user(worker["id"]) is None
        assert database.get_user_any(worker["id"])["enabled"] is False

        enabled = client.post(
            f"/people/{worker['id']}/status",
            data={"enabled": "true", "csrf": csrf},
            follow_redirects=False,
        )
        assert enabled.status_code == 303
        assert database.get_user(worker["id"])["enabled"] is True
        # Re-enabling an account does not silently restore a revoked device token.
        assert database.authenticate_device(token) is None


def test_team_schedule_requires_target_and_project_in_the_same_delegated_team(
    tmp_path, postgres_url
):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        owner = database.get_user_by_email("owner@example.test")
        lead = _member(database, owner, "lead@example.test")
        worker = _member(database, owner, "worker@example.test")
        linked = database.create_project("Linked project", "", owner["id"])
        unrelated = database.create_project("Other project", "", owner["id"])
        database.add_project_member(linked["id"], worker["id"])
        database.add_project_member(unrelated["id"], worker["id"])
        team_id = database.create_team("Engineering", lead["id"])
        database.add_team_member(team_id, worker["id"])
        database.add_team_project(team_id, linked["id"])
        database.set_team_lead_permissions(
            team_id, lead["id"], {"manage_schedules": True}
        )

        _login(client, lead["email"])
        csrf = _csrf(client.get("/schedules"))
        payload = {
            "user_id": worker["id"],
            "starts_at": "2026-09-10T09:00:00+00:00",
            "ends_at": "2026-09-10T17:00:00+00:00",
            "csrf": csrf,
        }
        denied = client.post(
            "/schedules", data={**payload, "project_id": unrelated["id"]}
        )
        allowed = client.post(
            "/schedules",
            data={**payload, "project_id": linked["id"]},
            follow_redirects=False,
        )

        assert denied.status_code == 404
        assert allowed.status_code == 303


def test_project_manager_cannot_delete_coworker_screenshot_even_when_self_delete_is_enabled(
    tmp_path, postgres_url
):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        owner = database.get_user_by_email("owner@example.test")
        manager = _member(database, owner, "manager@example.test")
        worker = _member(database, owner, "worker@example.test")
        project = database.create_project("Private activity", "", owner["id"])
        database.add_project_member(project["id"], manager["id"], "manager")
        database.add_project_member(project["id"], worker["id"], "worker")
        device, _ = database.create_device("Worker laptop", worker["id"], project["id"])
        record_id = str(uuid.uuid4())
        database.add_record(
            {
                "id": record_id,
                "device_id": device["id"],
                "captured_at": datetime.now(UTC).isoformat(),
                "keyboard_events": 2,
                "mouse_clicks": 1,
                "mouse_distance": 20,
                "active_app": "Editor",
                "agent_version": "test",
                "screenshot_path": "private.jpg",
                "focused_seconds": 300,
                "interactive_seconds": 100,
                "user_id": worker["id"],
                "project_id": project["id"],
                "activity_percent": 33,
            }
        )
        policy = database.organization_settings()
        policy["allow_screenshot_delete"] = True
        database.update_organization_settings(policy)

        _login(client, manager["email"])
        project_page = client.get(f"/projects/{project['id']}")
        response = client.post(
            f"/screenshots/{record_id}/delete",
            data={"csrf": _csrf(project_page)},
        )

        assert response.status_code == 403
        assert database.get_record(record_id) is not None


def test_device_location_replay_window_rejects_future_and_stale_events(
    tmp_path, postgres_url
):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        owner = database.get_user_by_email("owner@example.test")
        project = database.create_project("Location project", "", owner["id"])
        _, token = database.create_device("Field device", owner["id"], project["id"])
        headers = {"Authorization": f"Bearer {token}"}
        now = datetime.now(UTC)

        for recorded_at in (now + timedelta(hours=1), now - timedelta(days=91)):
            response = client.post(
                "/api/v1/location",
                headers=headers,
                json={
                    "event_id": str(uuid.uuid4()),
                    "recorded_at": recorded_at.isoformat(),
                    "latitude": 31.5,
                    "longitude": 74.3,
                    "accuracy_meters": 10,
                },
            )
            assert response.status_code == 422
            assert "replay window" in response.text


def test_browser_mutation_routes_keep_csrf_guards(tmp_path, postgres_url):
    app = create_app(_settings(tmp_path, postgres_url))
    exempt_prefixes = ("/api/v1/", "/scim/v2/")
    exempt_paths = {
        "/auth/saml/acs",
        "/api/v1/payroll/provider-callback",
        "/integrations/github/webhook",
    }
    preauthentication_paths = {
        "/login",
        "/invite/{token}",
        "/two-factor",
        "/two-factor/setup",
    }
    checked: list[str] = []

    routes = []
    for registered in app.routes:
        included = getattr(registered, "original_router", None)
        routes.extend(included.routes if included is not None else [registered])
    for route in routes:
        if not isinstance(route, APIRoute) or not route.methods.intersection(
            {"POST", "PUT", "PATCH", "DELETE"}
        ):
            continue
        if route.path in exempt_paths or route.path.startswith(exempt_prefixes):
            continue
        source = inspect.getsource(route.endpoint)
        assert "require_csrf" in source, f"{route.path} has no CSRF guard"
        if route.path not in preauthentication_paths:
            authentication_guards = (
                "require_user",
                "require_admin",
                "require_owner",
                "require_worker",
                "require_it_manager",
                "_require_project_",
                "_require_jira_",
                "_require_asana_",
            )
            assert any(guard in source for guard in authentication_guards), (
                f"{route.path} has no session authorization guard"
            )
        checked.append(route.path)

    assert len(checked) >= 35
    github_webhook = next(
        route
        for route in routes
        if isinstance(route, APIRoute) and route.path == "/integrations/github/webhook"
    )
    assert "verify_webhook" in inspect.getsource(github_webhook.endpoint)
    wise_webhook = next(
        route
        for route in routes
        if isinstance(route, APIRoute) and route.path == "/api/v1/payroll/wise-webhook"
    )
    wise_webhook_source = inspect.getsource(wise_webhook.endpoint)
    assert "handle_wise_webhook" in wise_webhook_source
    assert "X-Signature-SHA256" in wise_webhook_source
    saml_acs = next(
        route
        for route in routes
        if isinstance(route, APIRoute) and route.path == "/auth/saml/acs"
    )
    saml_source = inspect.getsource(saml_acs.endpoint)
    assert ".saml.authenticate" in saml_source
    assert "consume_saml_assertion" in saml_source

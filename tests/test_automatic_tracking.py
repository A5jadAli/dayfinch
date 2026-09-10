import re
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


def _member(database, admin, email="member@example.test"):
    _, token = database.create_invitation(email, admin["id"], 24)
    return database.accept_invitation(
        token, hash_password("member password long enough")
    )


def _csrf(response) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def _login(client: TestClient, email: str, password: str) -> None:
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf": _csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_agent_policy_requires_member_consent_and_exposes_only_assigned_policy(
    tmp_path, postgres_url
):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        admin = database.get_user_by_email("admin@example.test")
        member = _member(database, admin)
        outsider = _member(database, admin, "outsider@example.test")
        project = database.create_project("Automatic work", "", admin["id"])
        database.add_project_member(project["id"], member["id"])
        device, raw_token = database.create_device(
            "Member laptop", member["id"], project["id"]
        )
        _, outsider_token = database.create_device(
            "Outsider laptop", outsider["id"], project["id"]
        )
        with pytest.raises(ValueError, match="unavailable"):
            database.create_automatic_tracking_policy(
                "Invalid assignment",
                "fixed_schedule",
                project["id"],
                False,
                {"mon": [{"start": "09:00", "end": "17:00"}]},
                [outsider["id"]],
                admin["id"],
            )
        policy_id = database.create_automatic_tracking_policy(
            "Weekday policy",
            "fixed_schedule",
            project["id"],
            True,
            {"mon": [{"start": "09:00", "end": "17:00"}]},
            [member["id"]],
            admin["id"],
        )

        headers = {"Authorization": f"Bearer {raw_token}"}
        configuration = client.get("/api/v1/configuration", headers=headers)
        assert configuration.status_code == 200
        automatic = configuration.json()["automatic_tracking"]
        assert automatic == {
            "id": policy_id,
            "name": "Weekday policy",
            "rule_type": "fixed_schedule",
            "project_id": project["id"],
            "wait_for_activity": True,
            "schedule": {"mon": [{"start": "09:00", "end": "17:00"}]},
            "consent_status": "pending",
            "updated_at": automatic["updated_at"],
            "shift_windows": [],
        }
        assert (
            client.get(
                "/api/v1/configuration",
                headers={"Authorization": f"Bearer {outsider_token}"},
            ).json()["automatic_tracking"]
            is None
        )

        accepted = client.post(
            "/api/v1/automatic-tracking/consent",
            headers=headers,
            json={"policy_id": policy_id, "accepted": True},
        )
        assert accepted.status_code == 200
        assert accepted.json() == {"status": "accepted"}
        assert (
            client.get("/api/v1/configuration", headers=headers).json()[
                "automatic_tracking"
            ]["consent_status"]
            == "accepted"
        )

        wrong_member = client.post(
            "/api/v1/automatic-tracking/consent",
            headers={"Authorization": f"Bearer {outsider_token}"},
            json={"policy_id": policy_id, "accepted": True},
        )
        assert wrong_member.status_code == 404


def test_shift_policy_returns_bounded_published_windows_and_reassignment_resets_consent(
    tmp_path, postgres_url
):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app):
        database = app.state.database
        admin = database.get_user_by_email("admin@example.test")
        member = _member(database, admin)
        project = database.create_project("Shift work", "", admin["id"])
        database.add_project_member(project["id"], member["id"])
        now = datetime.now(UTC)
        database.add_shift(
            member["id"],
            project["id"],
            now + timedelta(hours=1),
            now + timedelta(hours=9),
            "",
            admin["id"],
        )
        first = database.create_automatic_tracking_policy(
            "First",
            "fixed_schedule",
            project["id"],
            False,
            {"tue": [{"start": "08:00", "end": "16:00"}]},
            [member["id"]],
            admin["id"],
        )
        assert database.respond_to_automatic_tracking_policy(member["id"], first, True)

        second = database.create_automatic_tracking_policy(
            "Shift based",
            "shifts",
            project["id"],
            False,
            {},
            [member["id"]],
            admin["id"],
        )
        policy = database.automatic_tracking_for_user(member["id"], now=now)

        assert policy["id"] == second
        assert policy["consent_status"] == "pending"
        assert len(policy["shift_windows"]) == 1
        assert policy["shift_windows"][0]["project_id"] == project["id"]
        policies = database.list_automatic_tracking_policies()
        assert (
            next(item for item in policies if item["id"] == first)["assignments"] == []
        )


def test_admin_can_create_and_delete_visible_policy_from_settings(
    tmp_path, postgres_url
):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        admin = database.get_user_by_email("admin@example.test")
        member = _member(database, admin)
        project = database.create_project("UI policy project", "", admin["id"])
        database.add_project_member(project["id"], member["id"])
        _login(client, "admin@example.test", "correct horse battery staple")
        settings = client.get("/settings")
        assert "Automatic desktop tracking" in settings.text

        created = client.post(
            "/settings/automatic-tracking",
            data={
                "csrf": _csrf(settings),
                "name": "UI weekday policy",
                "rule_type": "fixed_schedule",
                "project_id": project["id"],
                "start_time": "09:00",
                "end_time": "17:30",
                "day": ["mon", "tue", "wed", "thu", "fri"],
                "user_id": member["id"],
                "wait_for_activity": "on",
            },
            follow_redirects=False,
        )

        assert created.status_code == 303
        policy = database.list_automatic_tracking_policies()[0]
        assert policy["name"] == "UI weekday policy"
        assert policy["assignments"][0]["consent_status"] == "pending"
        assert database.respond_to_automatic_tracking_policy(
            member["id"], policy["id"], True
        )
        edit_page = client.get(f"/settings/automatic-tracking/{policy['id']}")
        assert edit_page.status_code == 200
        assert "Saving resets every assigned member" in edit_page.text
        edited = client.post(
            f"/settings/automatic-tracking/{policy['id']}",
            data={
                "csrf": _csrf(edit_page),
                "name": "Published shift policy",
                "rule_type": "shifts",
                "project_id": project["id"],
                "user_id": member["id"],
            },
            follow_redirects=False,
        )
        assert edited.status_code == 303
        policy = database.get_automatic_tracking_policy(policy["id"])
        assert policy["name"] == "Published shift policy"
        assert policy["rule_type"] == "shifts"
        assert policy["assignments"][0]["consent_status"] == "pending"
        refreshed = client.get("/settings")
        assert "Published shift policy" in refreshed.text
        deleted = client.post(
            f"/settings/automatic-tracking/{policy['id']}/delete",
            data={"csrf": _csrf(refreshed)},
            follow_redirects=False,
        )
        assert deleted.status_code == 303
        assert database.list_automatic_tracking_policies() == []

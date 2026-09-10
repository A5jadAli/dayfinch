from __future__ import annotations

import re

from fastapi.testclient import TestClient

from api.config import Settings
from api.main import create_app

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
TOKEN = "scim-secret-" + "x" * 32


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
        public_url="https://tracker.example.test",
        scim_bearer_token=TOKEN,
    )


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/scim+json",
    }


def _csrf(response) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def _enable_domain(database) -> None:
    values = database.organization_settings()
    values.update(sso_domain="example.test")
    database.update_organization_settings(values)


def test_scim_user_lifecycle_and_device_revocation(tmp_path, postgres_url):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        _enable_domain(database)
        unauthorized = client.get("/scim/v2/Users")
        assert unauthorized.status_code == 401
        assert unauthorized.headers["content-type"].startswith("application/scim+json")
        assert "Bearer" in unauthorized.headers["www-authenticate"]

        created = client.post(
            "/scim/v2/Users",
            headers=_headers(),
            json={
                "schemas": [USER_SCHEMA],
                "externalId": "idp-42",
                "userName": "person@example.test",
                "displayName": "Person Example",
                "active": True,
            },
        )
        assert created.status_code == 201
        resource = created.json()
        user_id = resource["id"]
        assert resource["active"] is True
        assert resource["meta"]["location"].startswith(
            "https://tracker.example.test/scim/v2/Users/"
        )
        assert created.headers["etag"].startswith('W/"')

        login_page = client.get("/login")
        password_login = client.post(
            "/login",
            data={
                "email": "person@example.test",
                "password": "not-a-password",
                "csrf": _csrf(login_page),
            },
        )
        assert password_login.status_code == 401

        found = client.get(
            "/scim/v2/Users?filter=userName%20eq%20%22person%40example.test%22",
            headers=_headers(),
        ).json()
        assert found["totalResults"] == 1
        assert found["Resources"][0]["id"] == user_id

        owner = database.get_user_by_email("admin@example.test")
        project = database.create_project("SCIM lifecycle", "", owner["id"])
        database.add_project_member(project["id"], user_id, "worker")
        device, _ = database.create_device("SCIM laptop", user_id, project["id"])
        active_session = database.sync_work_session(
            device, "active", None, project["id"]
        )
        disabled = client.patch(
            f"/scim/v2/Users/{user_id}",
            headers=_headers(),
            json={
                "schemas": [PATCH_SCHEMA],
                "Operations": [{"op": "Replace", "path": "active", "value": False}],
            },
        )
        assert disabled.status_code == 200
        assert disabled.json()["active"] is False
        assert database.get_user_by_email("person@example.test") is None
        assert database.get_device(device["id"])["enabled"] is False
        assert database.get_work_session(active_session["id"])["status"] == "stopped"

        enabled = client.patch(
            f"/scim/v2/Users/{user_id}",
            headers=_headers(),
            json={
                "schemas": [PATCH_SCHEMA],
                "Operations": [
                    {
                        "op": "replace",
                        "value": {"active": True, "displayName": "Renamed Person"},
                    }
                ],
            },
        )
        assert enabled.json()["displayName"] == "Renamed Person"
        assert enabled.json()["active"] is True

        deleted = client.delete(f"/scim/v2/Users/{user_id}", headers=_headers())
        assert deleted.status_code == 204
        assert database.get_scim_user(user_id)["enabled"] is False


def test_scim_rejects_duplicates_wrong_domains_and_unsupported_filters(
    tmp_path, postgres_url
):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        _enable_domain(app.state.database)
        body = {
            "schemas": [USER_SCHEMA],
            "externalId": "same-external-id",
            "userName": "person@example.test",
            "active": True,
        }
        assert (
            client.post("/scim/v2/Users", headers=_headers(), json=body).status_code
            == 201
        )
        duplicate = client.post("/scim/v2/Users", headers=_headers(), json=body)
        assert duplicate.status_code == 409
        assert duplicate.json()["scimType"] == "uniqueness"

        outside = client.post(
            "/scim/v2/Users",
            headers=_headers(),
            json={**body, "externalId": "other", "userName": "person@outside.test"},
        )
        assert outside.status_code == 400
        assert outside.json()["scimType"] == "invalidValue"

        invalid_filter = client.get(
            "/scim/v2/Users?filter=displayName%20eq%20%22Person%22",
            headers=_headers(),
        )
        assert invalid_filter.status_code == 400
        assert invalid_filter.json()["scimType"] == "invalidFilter"


def test_scim_discovery_describes_supported_features(tmp_path, postgres_url):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        config = client.get("/scim/v2/ServiceProviderConfig", headers=_headers())
        assert config.status_code == 200
        assert config.json()["patch"]["supported"] is True
        assert config.json()["filter"]["maxResults"] == 200
        resource_types = client.get("/scim/v2/ResourceTypes", headers=_headers())
        assert resource_types.status_code == 200
        assert {item["id"] for item in resource_types.json()} == {"User", "Group"}
        schemas = client.get("/scim/v2/Schemas", headers=_headers())
        assert schemas.status_code == 200
        assert {item["id"] for item in schemas.json()} == {USER_SCHEMA, GROUP_SCHEMA}


def test_scim_cannot_modify_workspace_owner(tmp_path, postgres_url):
    settings = _settings(tmp_path, postgres_url)
    app = create_app(settings)
    with TestClient(app) as client:
        _enable_domain(app.state.database)
        owner = app.state.database.get_user_by_email(settings.admin_email)

        hidden = client.get(f"/scim/v2/Users/{owner['id']}", headers=_headers())
        deletion = client.delete(f"/scim/v2/Users/{owner['id']}", headers=_headers())

        assert hidden.status_code == 404
        assert deletion.status_code == 404
        assert app.state.database.get_user(owner["id"])["enabled"] is True


def test_scim_group_lifecycle_synchronizes_team_members(tmp_path, postgres_url):
    settings = _settings(tmp_path, postgres_url)
    app = create_app(settings)
    with TestClient(app) as client:
        database = app.state.database
        _enable_domain(database)

        user_ids = []
        for number in (1, 2):
            created = client.post(
                "/scim/v2/Users",
                headers=_headers(),
                json={
                    "schemas": [USER_SCHEMA],
                    "externalId": f"person-{number}",
                    "userName": f"person{number}@example.test",
                    "active": True,
                },
            )
            assert created.status_code == 201
            user_ids.append(created.json()["id"])

        created = client.post(
            "/scim/v2/Groups",
            headers=_headers(),
            json={
                "schemas": [GROUP_SCHEMA],
                "externalId": "engineering-idp",
                "displayName": "Engineering",
                "members": [{"value": user_ids[0]}],
            },
        )
        assert created.status_code == 201
        group_id = created.json()["id"]
        assert [item["value"] for item in created.json()["members"]] == [user_ids[0]]
        assert database.get_team(group_id)["scim_managed"] is True

        added = client.patch(
            f"/scim/v2/Groups/{group_id}",
            headers=_headers(),
            json={
                "schemas": [PATCH_SCHEMA],
                "Operations": [
                    {"op": "Add", "path": "members", "value": [{"value": user_ids[1]}]}
                ],
            },
        )
        assert {item["value"] for item in added.json()["members"]} == set(user_ids)

        removed = client.patch(
            f"/scim/v2/Groups/{group_id}",
            headers=_headers(),
            json={
                "schemas": [PATCH_SCHEMA],
                "Operations": [
                    {"op": "Remove", "path": f'members[value eq "{user_ids[0]}"]'}
                ],
            },
        )
        assert [item["value"] for item in removed.json()["members"]] == [user_ids[1]]

        found = client.get(
            "/scim/v2/Groups?filter=displayName%20eq%20%22Engineering%22",
            headers=_headers(),
        ).json()
        assert found["totalResults"] == 1
        assert found["Resources"][0]["id"] == group_id

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
        people = client.get("/people")
        manual_change = client.post(
            f"/teams/{group_id}/members/{user_ids[1]}/remove",
            data={"csrf": _csrf(people)},
        )
        assert manual_change.status_code == 409

        deleted = client.delete(f"/scim/v2/Groups/{group_id}", headers=_headers())
        assert deleted.status_code == 204
        assert database.get_team(group_id) is None


def test_scim_group_rejects_privileged_or_unknown_members(tmp_path, postgres_url):
    settings = _settings(tmp_path, postgres_url)
    app = create_app(settings)
    with TestClient(app) as client:
        _enable_domain(app.state.database)
        owner = app.state.database.get_user_by_email(settings.admin_email)
        response = client.post(
            "/scim/v2/Groups",
            headers=_headers(),
            json={
                "schemas": [GROUP_SCHEMA],
                "displayName": "Privileged",
                "members": [{"value": owner["id"]}],
            },
        )
        assert response.status_code == 400
        assert response.json()["scimType"] == "invalidValue"
        assert app.state.database.list_teams() == []

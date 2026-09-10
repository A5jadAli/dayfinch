import base64
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from api.config import Settings
from api.main import create_app
from api.security import hash_password
from api.services.asana_integration import (
    ASANA_SCOPES,
    AsanaCloudService,
    AsanaIntegrationError,
)
from api.services.integration_credentials import (
    CredentialKeyring,
    IntegrationCredentialVault,
)


def _keyring_spec() -> str:
    return "primary:" + base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("=")


def _settings(tmp_path, postgres_url) -> Settings:
    settings = Settings(
        data_dir=tmp_path,
        database_url=postgres_url,
        max_upload_bytes=1024 * 1024,
        retention_days=30,
        session_secret="s" * 40,
        cookie_secure=False,
        admin_email="owner@example.test",
        admin_password="a-production-like-password",
        public_url="http://testserver",
        allowed_hosts=("testserver",),
        integration_encryption_keys=_keyring_spec(),
        asana_client_id="asana-client-id",
        asana_client_secret="asana-client-secret-value",
        asana_authorize_url="https://app.asana.test/-/oauth_authorize",
        asana_token_url="https://app.asana.test/-/oauth_token",
        asana_token_info_url="https://app.asana.test/-/token_info",
        asana_revoke_url="https://app.asana.test/-/oauth_revoke",
        asana_api_url="https://app.asana.test/api/1.0",
    )
    settings.prepare()
    return settings


class AsanaContract:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.refreshes = 0
        self.revocations = 0
        self.task_mode = "full"
        self.repeat_workspace_offset = False
        self.rate_limit_projects = False
        self.reject_projects_once = False
        self.identity_gid = "asana-owner-1"
        self.identity_name = "Dayfinch Owner"
        self.stories: dict[str, list[dict]] = {}
        self.next_story_id = 9000
        self.lose_next_story_response = False
        self.deny_stories = False
        self.invalid_story_page = False

    @staticmethod
    def _task(
        gid: str,
        name: str,
        *,
        completed: bool = False,
        assignee: str = "asana-owner-1",
        subtasks: int = 0,
    ) -> dict:
        return {
            "gid": gid,
            "name": name,
            "notes": f"Notes for {name}",
            "completed": completed,
            "modified_at": "2026-09-09T10:00:00.000Z",
            "permalink_url": f"https://app.asana.test/0/1/{gid}",
            "assignee": {"gid": assignee} if assignee else None,
            "num_subtasks": subtasks,
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/-/oauth_token":
            body = parse_qs(request.content.decode())
            if body.get("grant_type") == ["refresh_token"]:
                self.refreshes += 1
                return httpx.Response(
                    200,
                    json={
                        "access_token": "asana-refreshed-access-token",
                        "expires_in": 3600,
                        "token_type": "bearer",
                    },
                )
            assert len(body["code_verifier"][0]) >= 43
            token_owner = (
                "initial" if self.identity_gid == "asana-owner-1" else "member"
            )
            return httpx.Response(
                200,
                json={
                    "access_token": f"asana-{token_owner}-access-token",
                    "refresh_token": f"asana-{token_owner}-refresh-token",
                    "expires_in": 3600,
                    "token_type": "bearer",
                    "data": {
                        "gid": self.identity_gid,
                        "name": self.identity_name,
                        "email": "owner@example.test",
                    },
                },
            )
        if request.url.path == "/-/token_info":
            return httpx.Response(
                200,
                json={
                    "active": True,
                    "token_type": "bearer",
                    "scope": ASANA_SCOPES,
                },
            )
        if request.url.path == "/-/oauth_revoke":
            self.revocations += 1
            return httpx.Response(200, json={})
        if request.url.path == "/api/1.0/workspaces":
            offset = request.url.params.get("offset")
            if offset == "workspace-page-2":
                next_page = (
                    {"offset": "workspace-page-2"}
                    if self.repeat_workspace_offset
                    else None
                )
                return httpx.Response(
                    200,
                    json={
                        "data": [{"gid": "workspace-2", "name": "Studio"}],
                        "next_page": next_page,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": [{"gid": "workspace-1", "name": "Acme"}],
                    "next_page": {"offset": "workspace-page-2"},
                },
            )
        if request.url.path == "/api/1.0/workspaces/workspace-1/projects":
            if self.reject_projects_once:
                self.reject_projects_once = False
                return httpx.Response(401, json={"errors": [{"message": "Expired"}]})
            if self.rate_limit_projects:
                return httpx.Response(429, headers={"Retry-After": "37"}, json={})
            return httpx.Response(
                200,
                json={
                    "data": [{"gid": "project-1", "name": "Engineering"}],
                    "next_page": None,
                },
            )
        if request.url.path == "/api/1.0/projects/project-1/tasks":
            if self.task_mode == "missing-second":
                tasks = [self._task("task-1", "Build API", subtasks=1)]
            elif self.task_mode == "completed":
                tasks = [
                    self._task("task-1", "Build API", completed=True, subtasks=1),
                    self._task("task-2", "Review UI", completed=True, assignee=""),
                ]
            else:
                tasks = [
                    self._task("task-1", "Build API", subtasks=1),
                    self._task("task-2", "Review UI", assignee=""),
                ]
            return httpx.Response(200, json={"data": tasks, "next_page": None})
        batch_task_match = re.fullmatch(
            r"/api/1\.0/projects/batch-(\d+)/tasks", request.url.path
        )
        if batch_task_match:
            suffix = batch_task_match.group(1)
            return httpx.Response(
                200,
                json={
                    "data": [self._task(f"batch-task-{suffix}", f"Batch {suffix}")],
                    "next_page": None,
                },
            )
        if request.url.path == "/api/1.0/tasks/task-1/subtasks":
            return httpx.Response(
                200,
                json={
                    "data": [self._task("subtask-1", "Handle edge case")],
                    "next_page": None,
                },
            )
        story_list_match = re.fullmatch(
            r"/api/1\.0/tasks/([^/]+)/stories", request.url.path
        )
        if story_list_match:
            if self.deny_stories:
                return httpx.Response(403, json={"errors": [{"message": "Forbidden"}]})
            task_gid = story_list_match.group(1)
            stories = self.stories.setdefault(task_gid, [])
            if request.method == "GET":
                if self.invalid_story_page:
                    return httpx.Response(
                        200,
                        json={
                            "data": [
                                {
                                    "gid": "invalid-story",
                                    "resource_subtype": "comment_added",
                                    "text": "x" * 100_001,
                                    "created_by": {"gid": self.identity_gid},
                                }
                            ],
                            "next_page": None,
                        },
                    )
                offset = int(request.url.params.get("offset", "0"))
                page = stories[offset : offset + 100]
                next_page = (
                    {"offset": str(offset + len(page))}
                    if offset + len(page) < len(stories)
                    else None
                )
                return httpx.Response(200, json={"data": page, "next_page": next_page})
            if request.method == "POST":
                body = json.loads(request.content)
                self.next_story_id += 1
                story = {
                    "gid": str(self.next_story_id),
                    "resource_subtype": "comment_added",
                    "text": body["data"]["text"],
                    "created_by": {"gid": self.identity_gid},
                }
                stories.append(story)
                if self.lose_next_story_response:
                    self.lose_next_story_response = False
                    raise httpx.ReadError("story response lost", request=request)
                return httpx.Response(201, json={"data": story})
        story_match = re.fullmatch(r"/api/1\.0/stories/([^/]+)", request.url.path)
        if story_match:
            if self.deny_stories:
                return httpx.Response(403, json={"errors": [{"message": "Forbidden"}]})
            story_gid = story_match.group(1)
            story = next(
                (
                    item
                    for stories in self.stories.values()
                    for item in stories
                    if item["gid"] == story_gid
                ),
                None,
            )
            if not story:
                return httpx.Response(404, json={"errors": [{"message": "Missing"}]})
            body = json.loads(request.content)
            story["text"] = body["data"]["text"]
            return httpx.Response(200, json={"data": story})
        raise AssertionError(
            f"Unexpected Asana request: {request.method} {request.url}"
        )


def _service(tmp_path, postgres_url, database, contract: AsanaContract):
    settings = _settings(tmp_path, postgres_url)
    vault = IntegrationCredentialVault(
        database, CredentialKeyring.parse(settings.integration_encryption_keys)
    )
    service = AsanaCloudService(
        settings,
        database,
        vault,
        transport=httpx.MockTransport(contract),
        sleep=lambda _seconds: None,
    )
    return settings, vault, service


def _csrf(response) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def _login(
    client: TestClient,
    settings: Settings,
    *,
    email: str | None = None,
    password: str | None = None,
) -> None:
    client.cookies.clear()
    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "email": email or settings.admin_email,
            "password": password or settings.admin_password,
            "csrf": _csrf(page),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_asana_oauth_workspace_selection_task_and_subtask_reconciliation(
    tmp_path, postgres_url, database
):
    contract = AsanaContract()
    settings, vault, service = _service(tmp_path, postgres_url, database, contract)
    owner = database.bootstrap_admin(
        settings.admin_email, hash_password("test-password-long")
    )
    values, expires_at = service.exchange_code("authorization-code", "v" * 64)
    workspaces = service.workspaces_with_token(values["access_token"])
    assert [item["name"] for item in workspaces] == ["Acme", "Studio"]

    pending_id = service.store_pending_site_authorization(
        owner["id"], values, expires_at, workspaces
    )
    with database.connect() as connection:
        encrypted = connection.execute(
            "SELECT ciphertext FROM integration_oauth_pending WHERE id=%s",
            (pending_id,),
        ).fetchone()["ciphertext"]
        assert b"asana-initial-access-token" not in bytes(encrypted)
    assert (
        vault.open_pending(pending_id, owner["id"], "asana").values["workspaces"][0][
            "id"
        ]
        == "workspace-1"
    )

    rejected_pending_id = service.store_pending_site_authorization(
        owner["id"], values, expires_at, workspaces
    )
    with pytest.raises(AsanaIntegrationError) as mismatch:
        service.complete_site_authorization(
            rejected_pending_id, owner["id"], "workspace-not-granted"
        )
    assert mismatch.value.error_code == "workspace_mismatch"
    assert (
        database.get_pending_oauth_credentials(
            rejected_pending_id, owner["id"], "asana"
        )
        is None
    )

    integration_id = service.complete_site_authorization(
        pending_id, owner["id"], "workspace-1"
    )
    assert (
        database.get_pending_oauth_credentials(pending_id, owner["id"], "asana") is None
    )
    project = database.create_project("Asana Engineering", "", owner["id"])
    database.set_asana_project_mapping(
        integration_id, "project-1", "Engineering", project["id"]
    )
    assert service.sync_now(integration_id) == 3
    tasks = database.list_tasks(project["id"], include_archived=True)
    assert {task["external_display_key"] for task in tasks} == {
        "task-1",
        "task-2",
        "subtask-1",
    }
    assert all(task["external_read_only"] for task in tasks)
    assert {
        task["external_display_key"]
        for task in database.list_tasks(project["id"], user_id=owner["id"])
    } == {"task-1", "subtask-1"}
    with database.connect() as connection:
        assignees = connection.execute(
            """SELECT external_task_gid,asana_user_gid
               FROM asana_task_assignees ORDER BY external_task_gid"""
        ).fetchall()
    assert {row["external_task_gid"]: row["asana_user_gid"] for row in assignees} == {
        "subtask-1": "asana-owner-1",
        "task-1": "asana-owner-1",
        "task-2": "",
    }

    contract.task_mode = "missing-second"
    assert service.sync_now(integration_id) == 2
    assert (
        database.get_task(
            next(
                task["id"] for task in tasks if task["external_display_key"] == "task-2"
            )
        )["status"]
        == "active"
    )
    assert service.sync_now(integration_id) == 2
    assert (
        database.get_task(
            next(
                task["id"] for task in tasks if task["external_display_key"] == "task-2"
            )
        )["status"]
        == "archived"
    )

    contract.task_mode = "completed"
    assert service.sync_now(integration_id) == 3
    statuses = {
        task["external_display_key"]: task["status"]
        for task in database.list_tasks(project["id"], include_archived=True)
    }
    assert statuses["task-1"] == statuses["task-2"] == "archived"
    assert statuses["subtask-1"] == "active"

    service.revoke_stored(integration_id)
    database.disconnect_asana_integration(integration_id)
    assert contract.revocations == 1
    assert database.get_integration_credentials(integration_id, "asana") is None
    assert all(
        not task["external_read_only"]
        for task in database.list_tasks(project["id"], include_archived=True)
    )
    service.close()


def test_asana_refresh_identity_uniqueness_rate_limit_and_pagination_guards(
    tmp_path, postgres_url, database
):
    contract = AsanaContract()
    settings, _vault, service = _service(tmp_path, postgres_url, database, contract)
    owner = database.bootstrap_admin(
        settings.admin_email, hash_password("test-password-long")
    )
    values, _expires_at = service.exchange_code("authorization-code", "v" * 64)
    integration_id = database.upsert_asana_workspace("workspace-1", "Acme", owner["id"])
    service.store_authorization(
        integration_id, values, datetime.now(UTC) - timedelta(minutes=1)
    )
    database.upsert_asana_user_connection(
        integration_id,
        owner["id"],
        values["user_gid"],
        values["display_name"],
        "site",
    )
    assert service.access_token(integration_id) == "asana-refreshed-access-token"
    assert contract.refreshes == 1

    member = database.create_scim_user(
        "member@example.test", "asana-member-external", "Member", True
    )
    old_member_values = dict(values)
    old_member_values["access_token"] = "member-old-access"
    old_member_values["refresh_token"] = "member-old-refresh"
    service.store_authorization(
        integration_id,
        old_member_values,
        datetime.now(UTC) + timedelta(hours=1),
        subject_user_id=member["id"],
    )
    with pytest.raises(ValueError, match="already connected"):
        service.complete_member_authorization(
            integration_id,
            member["id"],
            values,
            datetime.now(UTC) + timedelta(hours=1),
        )
    assert (
        _vault.open(integration_id, "asana", subject_user_id=member["id"]).values[
            "refresh_token"
        ]
        == "member-old-refresh"
    )

    refreshes_before_early_rejection = contract.refreshes
    contract.reject_projects_once = True
    assert service.list_projects(integration_id)[0]["id"] == "project-1"
    assert contract.refreshes == refreshes_before_early_rejection + 1

    contract.rate_limit_projects = True
    with pytest.raises(AsanaIntegrationError) as rate_error:
        service.list_projects(integration_id)
    assert rate_error.value.error_code == "rate_limited"
    assert rate_error.value.retry_seconds == 37
    contract.rate_limit_projects = False

    contract.repeat_workspace_offset = True
    with pytest.raises(AsanaIntegrationError) as pagination_error:
        service.workspaces_with_token("asana-refreshed-access-token")
    assert pagination_error.value.error_code == "invalid_response"
    service.close()


def test_asana_time_comments_aggregate_recover_update_and_fail_closed(
    tmp_path, postgres_url, database
):
    contract = AsanaContract()
    settings, _vault, service = _service(tmp_path, postgres_url, database, contract)
    owner = database.bootstrap_admin(
        settings.admin_email, hash_password("test-password-long")
    )
    values, expires_at = service.exchange_code("authorization-code", "v" * 64)
    integration_id = database.upsert_asana_workspace("workspace-1", "Acme", owner["id"])
    service.store_authorization(integration_id, values, expires_at)
    database.upsert_asana_user_connection(
        integration_id,
        owner["id"],
        values["user_gid"],
        values["display_name"],
        "site",
    )
    project = database.create_project("Asana comments", "", owner["id"])
    database.set_asana_project_mapping(
        integration_id, "project-1", "Engineering", project["id"]
    )
    assert service.sync_now(integration_id) == 3
    task = next(
        task
        for task in database.list_tasks(project["id"], include_archived=True)
        if task["external_display_key"] == "task-1"
    )
    work_day = datetime.now(UTC).date() - timedelta(days=2)
    day_start = datetime.combine(work_day, datetime.min.time(), tzinfo=UTC)
    with database.connect() as connection:
        connection.execute(
            """UPDATE asana_user_connections
               SET connected_at=%s,next_comment_sync_at=%s,time_sync_mode='daily'
               WHERE integration_id=%s AND user_id=%s""",
            (day_start, datetime.now(UTC), integration_id, owner["id"]),
        )
        connection.execute(
            """UPDATE asana_user_authorization_periods SET started_at=%s
               WHERE integration_id=%s AND user_id=%s AND ended_at IS NULL""",
            (day_start, integration_id, owner["id"]),
        )
    device, _token = database.create_device(
        "Asana comment device", owner["id"], project["id"]
    )
    work_session = database.sync_work_session(
        device,
        "active",
        task["id"],
        project["id"],
        observed_at=day_start + timedelta(hours=9),
    )
    database.sync_work_session(
        device,
        "stopped",
        task["id"],
        project["id"],
        observed_at=day_start + timedelta(hours=10),
    )
    manual_id = database.add_manual_time(
        owner["id"],
        project["id"],
        task["id"],
        day_start + timedelta(hours=10),
        day_start + timedelta(hours=10, minutes=30),
        "Review",
        auto_approve=True,
    )

    contract.lose_next_story_response = True
    assert service.sync_due_comments() == 0
    assert len(contract.stories["task-1"]) == 1
    assert "01:30:00" in contract.stories["task-1"][0]["text"]
    with database.connect() as connection:
        export = connection.execute(
            """SELECT * FROM asana_comment_exports
               WHERE integration_id=%s AND user_id=%s AND external_task_gid='task-1'""",
            (integration_id, owner["id"]),
        ).fetchone()
        assert export["provider_story_gid"] is None
        assert export["last_error_code"] == "network_error"
        connection.execute(
            "UPDATE asana_comment_exports SET next_attempt_at=%s WHERE id=%s",
            (datetime.now(UTC), export["id"]),
        )

    duplicate = json.loads(json.dumps(contract.stories["task-1"][0]))
    duplicate["gid"] = "9999"
    contract.stories["task-1"].append(duplicate)
    assert service.sync_due_comments() == 0
    with database.connect() as connection:
        failed = connection.execute(
            "SELECT * FROM asana_comment_exports WHERE id=%s", (export["id"],)
        ).fetchone()
        assert failed["last_error_code"] == "duplicate_comments"
        connection.execute(
            "UPDATE asana_comment_exports SET next_attempt_at=%s WHERE id=%s",
            (datetime.now(UTC), export["id"]),
        )
    contract.stories["task-1"].remove(duplicate)
    assert service.sync_due_comments() == 1
    assert len(contract.stories["task-1"]) == 1

    with database.connect() as connection:
        connection.execute(
            """UPDATE asana_comment_exports
               SET provider_story_gid=NULL,synced_seconds=0,
                   synced_started_at=NULL,next_attempt_at=%s
               WHERE id=%s""",
            (datetime.now(UTC), export["id"]),
        )
    contract.invalid_story_page = True
    assert service.sync_due_comments() == 0
    with database.connect() as connection:
        invalid_story = connection.execute(
            "SELECT last_error_code FROM asana_comment_exports WHERE id=%s",
            (export["id"],),
        ).fetchone()
        assert invalid_story["last_error_code"] == "invalid_response"
        connection.execute(
            "UPDATE asana_comment_exports SET next_attempt_at=%s WHERE id=%s",
            (datetime.now(UTC), export["id"]),
        )
    contract.invalid_story_page = False
    assert service.sync_due_comments() == 1

    database.review_item("manual_time_entries", manual_id, owner["id"], "rejected")
    with database.connect() as connection:
        connection.execute(
            """UPDATE asana_user_connections SET next_comment_sync_at=%s
               WHERE integration_id=%s AND user_id=%s""",
            (datetime.now(UTC), integration_id, owner["id"]),
        )
    contract.deny_stories = True
    refreshes_before = contract.refreshes
    assert service.sync_due_comments() == 0
    with database.connect() as connection:
        denied = connection.execute(
            "SELECT * FROM asana_comment_exports WHERE id=%s", (export["id"],)
        ).fetchone()
        assert denied["last_error_code"] == "permission_denied"
        connection.execute(
            "UPDATE asana_comment_exports SET next_attempt_at=%s WHERE id=%s",
            (datetime.now(UTC), export["id"]),
        )
    assert contract.refreshes == refreshes_before
    contract.deny_stories = False
    assert service.sync_due_comments() == 1
    assert "01:00:00" in contract.stories["task-1"][0]["text"]
    with pytest.raises(ValueError, match="same Asana account"):
        database.upsert_asana_user_connection(
            integration_id,
            owner["id"],
            "different-asana-account",
            "Different Account",
            "site",
        )

    database.review_item("manual_time_entries", manual_id, owner["id"], "approved")
    with database.connect() as connection:
        connection.execute(
            """UPDATE asana_user_connections SET next_comment_sync_at=%s
               WHERE integration_id=%s AND user_id=%s""",
            (datetime.now(UTC), integration_id, owner["id"]),
        )
    database.stage_due_asana_comments(datetime.now(UTC))
    first_claim = "11111111-1111-4111-8111-111111111111"
    second_claim = "22222222-2222-4222-8222-222222222222"
    claimed = database.claim_due_asana_comment_exports(datetime.now(UTC), first_claim)
    assert len(claimed) == 1
    assert (
        database.claim_due_asana_comment_exports(datetime.now(UTC), second_claim) == []
    )
    assert database.mark_asana_comment_export_failed(
        claimed[0]["id"], first_claim, datetime.now(UTC), "test_release", 30
    )
    database.review_item("manual_time_entries", manual_id, owner["id"], "rejected")
    with database.connect() as connection:
        connection.execute(
            "DELETE FROM work_session_segments WHERE session_id=%s",
            (work_session["id"],),
        )
        connection.execute(
            """UPDATE asana_user_connections SET next_comment_sync_at=%s
               WHERE integration_id=%s AND user_id=%s""",
            (datetime.now(UTC), integration_id, owner["id"]),
        )
        connection.execute(
            "UPDATE asana_comment_exports SET next_attempt_at=%s WHERE id=%s",
            (datetime.now(UTC), export["id"]),
        )
    assert service.sync_due_comments() == 1
    assert "tracked time removed" in contract.stories["task-1"][0]["text"]
    service.close()


def test_asana_authorization_url_uses_state_pkce_and_least_scopes(
    tmp_path, postgres_url, database
):
    contract = AsanaContract()
    _settings_value, _vault, service = _service(
        tmp_path, postgres_url, database, contract
    )
    url = httpx.URL(service.authorization_url("s" * 48, "c" * 64))
    assert url.params["state"] == "s" * 48
    assert url.params["code_challenge"] == "c" * 64
    assert url.params["code_challenge_method"] == "S256"
    assert set(url.params["scope"].split()) == set(ASANA_SCOPES.split())
    service.close()


def test_asana_project_sync_is_bounded_and_resumes_next_mapping_batch(
    tmp_path, postgres_url, database
):
    contract = AsanaContract()
    settings, _vault, service = _service(tmp_path, postgres_url, database, contract)
    owner = database.bootstrap_admin(
        settings.admin_email, hash_password("test-password-long")
    )
    values, expires_at = service.exchange_code("authorization-code", "v" * 64)
    integration_id = database.upsert_asana_workspace("workspace-1", "Acme", owner["id"])
    service.store_authorization(integration_id, values, expires_at)
    database.upsert_asana_user_connection(
        integration_id,
        owner["id"],
        values["user_gid"],
        values["display_name"],
        "site",
    )
    for index in range(11):
        suffix = f"{index:02d}"
        project = database.create_project(f"Batch project {suffix}", "", owner["id"])
        database.set_asana_project_mapping(
            integration_id, f"batch-{suffix}", f"Batch {suffix}", project["id"]
        )

    assert service.sync_now(integration_id) == 10
    with database.connect() as connection:
        first = connection.execute(
            """SELECT COUNT(*) completed FROM asana_project_mappings
               WHERE integration_id=%s AND last_full_sync_at IS NOT NULL""",
            (integration_id,),
        ).fetchone()
        integration = connection.execute(
            "SELECT last_sync_at,next_sync_at FROM integrations WHERE id=%s",
            (integration_id,),
        ).fetchone()
    assert first["completed"] == 10
    assert integration["last_sync_at"] is None
    assert datetime.fromisoformat(str(integration["next_sync_at"])) <= datetime.now(UTC)

    assert service.sync_due() == 1
    with database.connect() as connection:
        completed = connection.execute(
            """SELECT COUNT(*) completed FROM asana_project_mappings
               WHERE integration_id=%s AND last_full_sync_at IS NOT NULL""",
            (integration_id,),
        ).fetchone()
        integration = connection.execute(
            "SELECT last_sync_at,next_sync_at FROM integrations WHERE id=%s",
            (integration_id,),
        ).fetchone()
    assert completed["completed"] == 11
    assert integration["last_sync_at"] is not None
    assert datetime.fromisoformat(str(integration["next_sync_at"])) > datetime.now(UTC)
    service.close()


def test_asana_member_comment_uses_subject_token_and_excludes_authorization_gap(
    tmp_path, postgres_url, database
):
    contract = AsanaContract()
    settings, _vault, service = _service(tmp_path, postgres_url, database, contract)
    owner = database.bootstrap_admin(
        settings.admin_email, hash_password("test-password-long")
    )
    owner_values, owner_expiry = service.exchange_code("owner-authorization", "v" * 64)
    integration_id = database.upsert_asana_workspace("workspace-1", "Acme", owner["id"])
    service.store_authorization(integration_id, owner_values, owner_expiry)
    database.upsert_asana_user_connection(
        integration_id,
        owner["id"],
        owner_values["user_gid"],
        owner_values["display_name"],
        "site",
    )
    project = database.create_project("Asana member comments", "", owner["id"])
    database.set_asana_project_mapping(
        integration_id, "project-1", "Engineering", project["id"]
    )
    assert service.sync_now(integration_id) == 3
    task = next(
        task
        for task in database.list_tasks(project["id"], include_archived=True)
        if task["external_display_key"] == "task-1"
    )

    member = database.create_scim_user(
        "asana-worker@example.test", "asana-worker-external", "Asana Worker", True
    )
    database.add_project_member(project["id"], member["id"])
    contract.identity_gid = "asana-member-2"
    contract.identity_name = "Asana Worker"
    member_values, member_expiry = service.exchange_code(
        "member-authorization", "v" * 64
    )
    service.store_authorization(
        integration_id,
        member_values,
        member_expiry,
        subject_user_id=member["id"],
    )
    database.upsert_asana_user_connection(
        integration_id,
        member["id"],
        member_values["user_gid"],
        member_values["display_name"],
        "member",
    )
    with database.connect() as connection:
        connection.execute(
            """UPDATE asana_task_assignees SET asana_user_gid=%s
               WHERE integration_id=%s AND external_task_gid='task-1'""",
            (member_values["user_gid"], integration_id),
        )
    assert {
        item["external_display_key"]
        for item in database.list_tasks(project["id"], user_id=member["id"])
    } == {"task-1"}

    work_day = datetime.now(UTC).date() - timedelta(days=2)
    day_start = datetime.combine(work_day, datetime.min.time(), tzinfo=UTC)
    with database.connect() as connection:
        connection.execute(
            """UPDATE asana_user_connections
               SET connected_at=%s,next_comment_sync_at=%s,time_sync_mode='daily'
               WHERE integration_id=%s AND user_id=%s""",
            (day_start, datetime.now(UTC), integration_id, member["id"]),
        )
        connection.execute(
            """UPDATE asana_user_authorization_periods
               SET started_at=%s,ended_at=%s
               WHERE integration_id=%s AND user_id=%s AND ended_at IS NULL""",
            (
                day_start,
                day_start + timedelta(minutes=10),
                integration_id,
                member["id"],
            ),
        )
        connection.execute(
            """INSERT INTO asana_user_authorization_periods(
                   id,integration_id,user_id,asana_user_gid,started_at,created_at
               ) VALUES (gen_random_uuid(),%s,%s,%s,%s,%s)""",
            (
                integration_id,
                member["id"],
                member_values["user_gid"],
                day_start + timedelta(minutes=20),
                day_start + timedelta(minutes=20),
            ),
        )
    device, _token = database.create_device(
        "Asana member device", member["id"], project["id"]
    )
    database.sync_work_session(
        device,
        "active",
        task["id"],
        project["id"],
        observed_at=day_start,
    )
    database.sync_work_session(
        device,
        "stopped",
        task["id"],
        project["id"],
        observed_at=day_start + timedelta(minutes=30),
    )
    assert service.sync_due_comments() == 1
    assert "00:20:00" in contract.stories["task-1"][0]["text"]
    creation = next(
        request
        for request in contract.requests
        if request.method == "POST"
        and request.url.path == "/api/1.0/tasks/task-1/stories"
    )
    assert creation.headers["Authorization"] == "Bearer asana-member-access-token"
    service.close()


def test_asana_owner_web_flow_selects_workspace_maps_and_syncs(tmp_path, postgres_url):
    contract = AsanaContract()
    settings = _settings(tmp_path, postgres_url)
    app = create_app(settings, asana_transport=httpx.MockTransport(contract))
    with TestClient(app) as client:
        _login(client, settings)
        connect = client.get("/integrations/asana/connect", follow_redirects=False)
        assert connect.status_code == 303
        provider_url = httpx.URL(connect.headers["location"])
        assert provider_url.params["code_challenge_method"] == "S256"
        callback = client.get(
            "/integrations/asana/callback",
            params={
                "code": "authorization-code",
                "state": provider_url.params["state"],
            },
            follow_redirects=False,
        )
        assert callback.status_code == 303
        assert callback.headers["location"].startswith("/integrations/asana/select")
        selection = client.get(callback.headers["location"])
        assert selection.status_code == 200
        assert "Acme" in selection.text and "Studio" in selection.text
        assert "asana-initial-access-token" not in selection.text
        selected = client.post(
            "/integrations/asana/select",
            data={
                "csrf": _csrf(selection),
                "pending_id": re.search(
                    r'name="pending_id" value="([^"]+)"', selection.text
                ).group(1),
                "workspace_id": "workspace-1",
            },
            follow_redirects=False,
        )
        assert selected.status_code == 303
        integration_id = selected.headers["location"].rsplit("/", 1)[-1]
        integration_page = client.get(selected.headers["location"])
        assert integration_page.status_code == 200
        assert "Engineering" in integration_page.text
        owner = app.state.database.get_user_by_email(settings.admin_email)
        project = app.state.database.create_project(
            "Web Asana project", "", owner["id"]
        )
        mapped = client.post(
            selected.headers["location"] + "/mappings",
            data={
                "csrf": _csrf(integration_page),
                "external_project_id": "project-1",
                "project_id": project["id"],
            },
            follow_redirects=False,
        )
        assert mapped.status_code == 303
        mapped_page = client.get(mapped.headers["location"])
        synchronized = client.post(
            selected.headers["location"] + "/sync",
            data={"csrf": _csrf(mapped_page)},
            follow_redirects=False,
        )
        assert synchronized.status_code == 303
        assert len(app.state.database.list_tasks(project["id"])) == 3

        _, member_token = app.state.database.create_invitation(
            "asana-worker@example.test", owner["id"], 24
        )
        member = app.state.database.accept_invitation(
            member_token, hash_password("member password long enough")
        )
        app.state.database.add_project_member(project["id"], member["id"], "worker")
        _login(
            client,
            settings,
            email=member["email"],
            password="member password long enough",
        )
        account_page = client.get(f"/integrations/asana/{integration_id}/account")
        assert account_page.status_code == 200
        member_csrf = _csrf(account_page)
        assert client.get(f"/integrations/asana/{integration_id}").status_code == 403
        assert (
            client.post(
                f"/integrations/asana/{integration_id}/sync",
                data={"csrf": member_csrf},
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/integrations/asana/{integration_id}/disconnect",
                data={"csrf": member_csrf},
            ).status_code
            == 403
        )

        _, outsider_token = app.state.database.create_invitation(
            "asana-outsider@example.test", owner["id"], 24
        )
        outsider = app.state.database.accept_invitation(
            outsider_token, hash_password("member password long enough")
        )
        _login(
            client,
            settings,
            email=outsider["email"],
            password="member password long enough",
        )
        assert (
            client.get(f"/integrations/asana/{integration_id}/account").status_code
            == 403
        )

        invalid_state = client.get(
            "/integrations/asana/callback",
            params={"code": "authorization-code", "state": "wrong"},
        )
        assert invalid_state.status_code == 403

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from api.config import Settings
from api.main import create_app
from api.security import hash_password
from api.services.integration_credentials import (
    CredentialKeyring,
    IntegrationCredentialVault,
)
from api.services.jira_integration import JiraCloudService, JiraIntegrationError


class _OversizedResponseStream(httpx.SyncByteStream):
    def __iter__(self):
        chunk = b"x" * (4 * 1024 * 1024)
        yield chunk
        yield chunk
        yield b"x"


def _keyring_spec() -> str:
    return "primary:" + base64.urlsafe_b64encode(b"j" * 32).decode().rstrip("=")


def _settings(tmp_path, postgres_url: str) -> Settings:
    settings = Settings(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024 * 1024,
        retention_days=30,
        admin_email="owner@example.test",
        database_url=postgres_url,
        public_url="http://testserver",
        integration_encryption_keys=_keyring_spec(),
        jira_client_id="jira-client-id",
        jira_client_secret="jira-client-secret-value",
        jira_authorize_url="https://auth.jira.test/authorize",
        jira_token_url="https://auth.jira.test/oauth/token",
        jira_api_url="https://api.jira.test",
    )
    settings.prepare()
    return settings


class JiraContract:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.search_calls = 0
        self.refreshes = 0
        self.issue_mode = "full"
        self.identity_account_id = "712020:owner-atlassian-account"
        self.identity_display_name = "Dayfinch Owner"
        self.worklogs: dict[str, list[dict]] = {}
        self.worklog_properties: dict[tuple[str, str, str], object] = {}
        self.next_worklog_id = 50000
        self.lose_next_worklog_response = False
        self.deny_worklogs = False
        self.worklog_list_override: dict | None = None

    @staticmethod
    def _issue(
        issue_id: str,
        key: str,
        summary: str,
        category: str,
        updated: str,
    ) -> dict:
        return {
            "id": issue_id,
            "key": key,
            "fields": {
                "summary": summary,
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {"type": "text", "text": "Acceptance criteria"}
                            ],
                        }
                    ],
                },
                "status": {"statusCategory": {"key": category}},
                "updated": updated,
            },
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url == httpx.URL("https://auth.jira.test/oauth/token"):
            body = json.loads(request.content)
            if body["grant_type"] == "refresh_token":
                self.refreshes += 1
                return httpx.Response(
                    200,
                    json={
                        "access_token": "a-new-access-token-value-long-enough",
                        "refresh_token": "a-new-refresh-token-value-long-enough",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "scope": "offline_access read:jira-user read:jira-work write:jira-work",
                    },
                )
            assert (
                body["redirect_uri"] == "http://testserver/integrations/jira/callback"
            )
            token_owner = (
                "initial"
                if self.identity_account_id == "712020:owner-atlassian-account"
                else "member"
            )
            return httpx.Response(
                200,
                json={
                    "access_token": f"{token_owner}-access-token-value-long-enough",
                    "refresh_token": f"{token_owner}-refresh-token-value-long-enough",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "offline_access read:jira-user read:jira-work write:jira-work",
                },
            )
        if request.url.path == "/oauth/token/accessible-resources":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "cloud-123",
                        "name": "Acme Jira",
                        "url": "https://acme.atlassian.net",
                        "scopes": [
                            "offline_access",
                            "read:jira-user",
                            "read:jira-work",
                            "write:jira-work",
                        ],
                    }
                ],
            )
        if request.url.path.endswith("/rest/api/3/myself"):
            return httpx.Response(
                200,
                json={
                    "accountId": self.identity_account_id,
                    "accountType": "atlassian",
                    "active": True,
                    "displayName": self.identity_display_name,
                },
            )
        if request.url.path.endswith("/rest/api/3/project/search"):
            return httpx.Response(
                200,
                json={
                    "startAt": 0,
                    "maxResults": 50,
                    "total": 1,
                    "isLast": True,
                    "values": [{"id": "10001", "key": "ENG", "name": "Engineering"}],
                },
            )
        if request.url.path.endswith("/rest/api/3/search/jql"):
            body = json.loads(request.content)
            self.search_calls += 1
            if self.issue_mode == "incremental":
                assert "updated >=" in body["jql"]
                return httpx.Response(
                    200,
                    json={
                        "issues": [
                            self._issue(
                                "20002",
                                "ENG-2",
                                "Completed issue",
                                "done",
                                "2026-09-09T11:00:00.000+0000",
                            )
                        ]
                    },
                )
            if body.get("nextPageToken") == "page-two":
                return httpx.Response(
                    200,
                    json={
                        "issues": [
                            self._issue(
                                "20002",
                                "ENG-2",
                                "Completed issue",
                                "done",
                                "2026-09-09T10:00:00.000+0000",
                            )
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "issues": [
                        self._issue(
                            "20001",
                            "ENG-1",
                            "Build connector",
                            "indeterminate",
                            "2026-09-09T09:00:00.000+0000",
                        )
                    ],
                    "nextPageToken": "page-two",
                },
            )
        worklog_match = re.search(
            r"/rest/api/3/issue/([^/]+)/worklog(?:/([0-9]+))?"
            r"(?:/properties/([^/]+))?$",
            request.url.path,
        )
        if worklog_match:
            if self.deny_worklogs:
                return httpx.Response(403, json={"errorMessages": ["Forbidden"]})
            issue_key, worklog_id, property_key = worklog_match.groups()
            issue_worklogs = self.worklogs.setdefault(issue_key, [])
            if request.method == "GET" and property_key and worklog_id:
                property_value = self.worklog_properties.get(
                    (issue_key, worklog_id, property_key)
                )
                if property_value is None:
                    return httpx.Response(404, json={"errorMessages": ["Not found"]})
                return httpx.Response(
                    200, json={"key": property_key, "value": property_value}
                )
            if request.method == "GET" and not worklog_id:
                if self.worklog_list_override is not None:
                    return httpx.Response(200, json=self.worklog_list_override)
                start_at = int(request.url.params.get("startAt", "0"))
                max_results = int(request.url.params.get("maxResults", "100"))
                page = issue_worklogs[start_at : start_at + max_results]
                return httpx.Response(
                    200,
                    json={
                        "startAt": start_at,
                        "maxResults": max_results,
                        "total": len(issue_worklogs),
                        "worklogs": page,
                    },
                )
            if request.method == "POST" and not worklog_id:
                body = json.loads(request.content)
                self.next_worklog_id += 1
                created_id = str(self.next_worklog_id)
                for item in body.get("properties", []):
                    self.worklog_properties[(issue_key, created_id, item["key"])] = (
                        item["value"]
                    )
                worklog = {
                    "id": created_id,
                    "author": {"accountId": self.identity_account_id},
                    "started": body["started"],
                    "timeSpentSeconds": body["timeSpentSeconds"],
                    "properties": [
                        {"key": item["key"]} for item in body.get("properties", [])
                    ],
                }
                issue_worklogs.append(worklog)
                if self.lose_next_worklog_response:
                    self.lose_next_worklog_response = False
                    raise httpx.ReadError("worklog response lost", request=request)
                return httpx.Response(201, json=worklog)
            existing = next(
                (item for item in issue_worklogs if item["id"] == worklog_id), None
            )
            if not existing:
                return httpx.Response(404, json={"errorMessages": ["Not found"]})
            if request.method == "PUT":
                body = json.loads(request.content)
                existing["started"] = body["started"]
                existing["timeSpentSeconds"] = body["timeSpentSeconds"]
                for item in body.get("properties", []):
                    self.worklog_properties[(issue_key, worklog_id, item["key"])] = (
                        item["value"]
                    )
                existing["properties"] = [
                    {"key": item["key"]} for item in body.get("properties", [])
                ]
                return httpx.Response(200, json=existing)
            if request.method == "DELETE":
                issue_worklogs.remove(existing)
                for key in list(self.worklog_properties):
                    if key[0] == issue_key and key[1] == worklog_id:
                        del self.worklog_properties[key]
                return httpx.Response(204)
        raise AssertionError(f"Unexpected Jira request: {request.method} {request.url}")


def _service(tmp_path, postgres_url, database, contract: JiraContract):
    settings = _settings(tmp_path, postgres_url)
    vault = IntegrationCredentialVault(
        database, CredentialKeyring.parse(settings.integration_encryption_keys)
    )
    service = JiraCloudService(
        settings,
        database,
        vault,
        transport=httpx.MockTransport(contract),
        sleep=lambda _seconds: None,
    )
    return settings, vault, service


def _csrf(response) -> str:
    matched = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert matched, response.text
    return matched.group(1)


def _login(client: TestClient, email: str, password: str) -> None:
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf": _csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_jira_oauth_project_mapping_sync_refresh_and_access_cleanup(
    tmp_path, postgres_url, database
):
    contract = JiraContract()
    settings, vault, service = _service(tmp_path, postgres_url, database, contract)
    owner = database.bootstrap_admin(settings.admin_email, "hash")

    authorization = service.authorization_url("s" * 43)
    query = parse_qs(urlparse(authorization).query)
    assert query == {
        "audience": ["api.atlassian.com"],
        "client_id": [settings.jira_client_id],
        "scope": ["offline_access read:jira-user read:jira-work write:jira-work"],
        "redirect_uri": ["http://testserver/integrations/jira/callback"],
        "state": ["s" * 43],
        "response_type": ["code"],
        "prompt": ["consent"],
    }
    values, expires_at = service.exchange_code("authorization-code-value")
    resources = service.accessible_resources(values["access_token"])
    assert resources == [
        {
            "id": "cloud-123",
            "name": "Acme Jira",
            "url": "https://acme.atlassian.net",
        }
    ]
    integration_id = database.upsert_jira_site(
        "cloud-123", "Acme Jira", "https://acme.atlassian.net", owner["id"]
    )
    service.store_authorization(integration_id, values, expires_at)
    row = database.get_integration_credentials(integration_id, "jira")
    assert row is not None
    assert b"initial-refresh-token" not in bytes(row["ciphertext"])

    project = database.create_project("Dayfinch engineering", "", owner["id"])
    assert service.list_projects(integration_id) == [
        {"id": "10001", "key": "ENG", "name": "Engineering"}
    ]
    database.set_jira_project_mapping(
        integration_id, "10001", "ENG", "Engineering", project["id"]
    )
    assert service.sync_now(integration_id) == 2
    with database.connect() as connection:
        tasks = connection.execute(
            "SELECT * FROM tasks WHERE integration_id=%s ORDER BY external_key",
            (integration_id,),
        ).fetchall()
    assert [task["name"] for task in tasks] == [
        "ENG-1: Build connector",
        "ENG-2: Completed issue",
    ]
    assert tasks[0]["description"] == "Acceptance criteria"
    assert tasks[0]["status"] == "active"
    assert tasks[0]["external_url"] == "https://acme.atlassian.net/browse/ENG-1"
    assert tasks[1]["status"] == "archived"
    mapping = database.jira_project_mappings(integration_id)[0]
    assert mapping["last_full_sync_at"] and mapping["last_incremental_sync_at"]

    contract.issue_mode = "incremental"
    assert service.sync_now(integration_id) == 1
    assert database.get_task(tasks[0]["id"])["status"] == "active"
    assert database.get_task(tasks[1]["id"])["status"] == "archived"

    database.expire_integration_access(integration_id, "jira", datetime.now(UTC))
    assert (
        service.access_token(integration_id) == "a-new-access-token-value-long-enough"
    )
    assert contract.refreshes == 1
    assert (
        vault.open(integration_id, "jira")
        .values["refresh_token"]
        .startswith("a-new-refresh")
    )

    device, _ = database.create_device("Owner workstation", owner["id"], project["id"])
    session = database.sync_work_session(
        device, "active", tasks[0]["id"], project["id"]
    )
    database.remove_jira_project_mapping(integration_id, "10001")
    assert database.get_task(tasks[0]["id"])["status"] == "archived"
    assert database.get_work_session(session["id"])["status"] == "stopped"
    service.close()


def test_jira_worklogs_are_aggregated_updated_and_recovered_after_lost_response(
    tmp_path, postgres_url, database
):
    contract = JiraContract()
    settings, _vault, service = _service(tmp_path, postgres_url, database, contract)
    owner = database.bootstrap_admin(settings.admin_email, "hash")
    values, expires_at = service.exchange_code("authorization-code-value")
    integration_id = database.upsert_jira_site(
        "cloud-123", "Acme Jira", "https://acme.atlassian.net", owner["id"]
    )
    service.store_authorization(integration_id, values, expires_at)
    identity = service.current_user("cloud-123", values["access_token"])
    database.upsert_jira_user_connection(
        integration_id,
        owner["id"],
        identity["account_id"],
        identity["display_name"],
        "site",
    )
    project = database.create_project("Jira worklogs", "", owner["id"])
    database.set_jira_project_mapping(
        integration_id, "10001", "ENG", "Engineering", project["id"]
    )
    assert service.sync_now(integration_id) == 2
    with database.connect() as connection:
        task = connection.execute(
            """SELECT * FROM tasks WHERE integration_id=%s
               AND external_display_key='ENG-1'""",
            (integration_id,),
        ).fetchone()
        work_day = datetime.now(UTC).date() - timedelta(days=2)
        day_start = datetime.combine(work_day, datetime.min.time(), tzinfo=UTC)
        connection.execute(
            """UPDATE jira_user_connections
               SET connected_at=%s,next_worklog_sync_at=%s,time_sync_mode='daily'
               WHERE integration_id=%s AND user_id=%s""",
            (day_start, datetime.now(UTC), integration_id, owner["id"]),
        )
        connection.execute(
            """UPDATE jira_user_authorization_periods SET started_at=%s
               WHERE integration_id=%s AND user_id=%s AND ended_at IS NULL""",
            (day_start, integration_id, owner["id"]),
        )

    device, _token = database.create_device(
        "Jira worklog device", owner["id"], project["id"]
    )
    database.sync_work_session(
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
        "Code review",
        auto_approve=True,
    )

    contract.lose_next_worklog_response = True
    assert service.sync_due_worklogs() == 0
    assert len(contract.worklogs["ENG-1"]) == 1
    with database.connect() as connection:
        failed = connection.execute(
            "SELECT * FROM jira_worklog_exports WHERE task_id=%s", (task["id"],)
        ).fetchone()
        assert failed["provider_worklog_id"] is None
        assert failed["last_error_code"] == "network_error"
        connection.execute(
            """UPDATE jira_worklog_exports SET next_attempt_at=%s
               WHERE id=%s""",
            (datetime.now(UTC), failed["id"]),
        )

    original_worklog = contract.worklogs["ENG-1"][0]
    duplicate_worklog = json.loads(json.dumps(original_worklog))
    duplicate_worklog["id"] = "59999"
    contract.worklogs["ENG-1"].append(duplicate_worklog)
    contract.worklog_properties[("ENG-1", "59999", "dayfinch.export_id")] = (
        contract.worklog_properties[
            ("ENG-1", original_worklog["id"], "dayfinch.export_id")
        ]
    )
    assert service.sync_due_worklogs() == 0
    with database.connect() as connection:
        duplicate_error = connection.execute(
            "SELECT * FROM jira_worklog_exports WHERE id=%s", (failed["id"],)
        ).fetchone()
        assert duplicate_error["last_error_code"] == "duplicate_worklogs"
        connection.execute(
            "UPDATE jira_worklog_exports SET next_attempt_at=%s WHERE id=%s",
            (datetime.now(UTC), failed["id"]),
        )
    contract.worklogs["ENG-1"].remove(duplicate_worklog)
    del contract.worklog_properties[("ENG-1", "59999", "dayfinch.export_id")]
    assert service.sync_due_worklogs() == 1
    assert len(contract.worklogs["ENG-1"]) == 1
    assert contract.worklogs["ENG-1"][0]["timeSpentSeconds"] == 5400

    # A hostile or corrupted provider response must fail closed before a POST.
    # Losing the locally remembered provider ID is an expected recovery path,
    # so exercise both the hard daily bound and strict item validation there.
    hostile_pages = [
        {"startAt": 0, "total": 10_001, "worklogs": []},
        {
            "startAt": 0,
            "total": 1,
            "worklogs": [
                {
                    "id": "../../not-a-worklog-id",
                    "author": {"accountId": contract.identity_account_id},
                    "properties": [],
                }
            ],
        },
    ]
    for hostile_page in hostile_pages:
        with database.connect() as connection:
            connection.execute(
                """UPDATE jira_worklog_exports
                       SET provider_worklog_id=NULL,synced_seconds=0,
                           synced_started_at=NULL,next_attempt_at=%s
                       WHERE id=%s""",
                (datetime.now(UTC), failed["id"]),
            )
        contract.worklog_list_override = hostile_page
        assert service.sync_due_worklogs() == 0
        with database.connect() as connection:
            hostile_error = connection.execute(
                "SELECT last_error_code FROM jira_worklog_exports WHERE id=%s",
                (failed["id"],),
            ).fetchone()
            assert hostile_error["last_error_code"] == "invalid_response"
        assert len(contract.worklogs["ENG-1"]) == 1

    contract.worklog_list_override = None
    with database.connect() as connection:
        connection.execute(
            "UPDATE jira_worklog_exports SET next_attempt_at=%s WHERE id=%s",
            (datetime.now(UTC), failed["id"]),
        )
    assert service.sync_due_worklogs() == 1
    assert len(contract.worklogs["ENG-1"]) == 1
    with pytest.raises(ValueError, match="same Jira account"):
        database.upsert_jira_user_connection(
            integration_id,
            owner["id"],
            "712020:different-owner",
            "Different Owner",
            "site",
        )

    database.review_item("manual_time_entries", manual_id, owner["id"], "rejected")
    with database.connect() as connection:
        connection.execute(
            """UPDATE jira_user_connections SET next_worklog_sync_at=%s
               WHERE integration_id=%s AND user_id=%s""",
            (datetime.now(UTC), integration_id, owner["id"]),
        )
    assert service.sync_due_worklogs() == 1
    assert len(contract.worklogs["ENG-1"]) == 1
    assert contract.worklogs["ENG-1"][0]["timeSpentSeconds"] == 3600

    removed_worklog_id = contract.worklogs["ENG-1"][0]["id"]
    contract.worklogs["ENG-1"].clear()
    for property_identity in list(contract.worklog_properties):
        if (
            property_identity[0] == "ENG-1"
            and property_identity[1] == removed_worklog_id
        ):
            del contract.worklog_properties[property_identity]
    database.review_item("manual_time_entries", manual_id, owner["id"], "approved")
    with database.connect() as connection:
        connection.execute(
            """UPDATE jira_user_connections SET next_worklog_sync_at=%s
               WHERE integration_id=%s AND user_id=%s""",
            (datetime.now(UTC), integration_id, owner["id"]),
        )
    assert service.sync_due_worklogs() == 1
    assert len(contract.worklogs["ENG-1"]) == 1
    assert contract.worklogs["ENG-1"][0]["timeSpentSeconds"] == 5400

    manual_only_task = database.apply_jira_issue(
        integration_id,
        "10001",
        {
            "id": "20003",
            "key": "ENG-3",
            "summary": "Manual-only work",
            "description": "",
            "done": False,
            "updated_at": datetime.now(UTC),
            "url": "https://acme.atlassian.net/browse/ENG-3",
        },
        datetime.now(UTC),
    )
    manual_only_id = database.add_manual_time(
        owner["id"],
        project["id"],
        manual_only_task,
        day_start + timedelta(hours=13),
        day_start + timedelta(hours=13, minutes=15),
        "Planning",
        auto_approve=True,
    )
    with database.connect() as connection:
        connection.execute(
            """UPDATE jira_user_connections SET next_worklog_sync_at=%s
               WHERE integration_id=%s AND user_id=%s""",
            (datetime.now(UTC), integration_id, owner["id"]),
        )
    assert service.sync_due_worklogs() == 1
    assert contract.worklogs["ENG-3"][0]["timeSpentSeconds"] == 900
    database.review_item("manual_time_entries", manual_only_id, owner["id"], "rejected")
    with database.connect() as connection:
        connection.execute(
            """UPDATE jira_user_connections SET next_worklog_sync_at=%s
               WHERE integration_id=%s AND user_id=%s""",
            (datetime.now(UTC), integration_id, owner["id"]),
        )
    assert service.sync_due_worklogs() == 1
    assert contract.worklogs["ENG-3"] == []

    midnight_task = database.apply_jira_issue(
        integration_id,
        "10001",
        {
            "id": "20004",
            "key": "ENG-4",
            "summary": "Midnight boundary",
            "description": "",
            "done": False,
            "updated_at": datetime.now(UTC),
            "url": "https://acme.atlassian.net/browse/ENG-4",
        },
        datetime.now(UTC),
    )
    database.add_manual_time(
        owner["id"],
        project["id"],
        midnight_task,
        day_start + timedelta(hours=23, minutes=30),
        day_start + timedelta(days=1, minutes=30),
        "Across UTC midnight",
        auto_approve=True,
    )
    with database.connect() as connection:
        connection.execute(
            """UPDATE jira_user_connections SET next_worklog_sync_at=%s
               WHERE integration_id=%s AND user_id=%s""",
            (datetime.now(UTC), integration_id, owner["id"]),
        )
    assert service.sync_due_worklogs() == 2
    assert sorted(
        worklog["timeSpentSeconds"] for worklog in contract.worklogs["ENG-4"]
    ) == [1800, 1800]

    denied_task = database.apply_jira_issue(
        integration_id,
        "10001",
        {
            "id": "20005",
            "key": "ENG-5",
            "summary": "Permission retry",
            "description": "",
            "done": False,
            "updated_at": datetime.now(UTC),
            "url": "https://acme.atlassian.net/browse/ENG-5",
        },
        datetime.now(UTC),
    )
    database.add_manual_time(
        owner["id"],
        project["id"],
        denied_task,
        day_start + timedelta(hours=15),
        day_start + timedelta(hours=15, minutes=10),
        "Permission failure",
        auto_approve=True,
    )
    with database.connect() as connection:
        connection.execute(
            """UPDATE jira_user_connections SET next_worklog_sync_at=%s
               WHERE integration_id=%s AND user_id=%s""",
            (datetime.now(UTC), integration_id, owner["id"]),
        )
    contract.deny_worklogs = True
    refreshes_before = contract.refreshes
    assert service.sync_due_worklogs() == 0
    assert contract.refreshes == refreshes_before
    with database.connect() as connection:
        denied_export = connection.execute(
            """SELECT * FROM jira_worklog_exports WHERE task_id=%s""",
            (denied_task,),
        ).fetchone()
        assert denied_export["last_error_code"] == "permission_denied"
        connection.execute(
            "UPDATE jira_worklog_exports SET next_attempt_at=%s WHERE id=%s",
            (datetime.now(UTC), denied_export["id"]),
        )
    first_claim = str(uuid4())
    second_claim = str(uuid4())
    first_rows = database.claim_due_jira_worklog_exports(datetime.now(UTC), first_claim)
    assert [row["id"] for row in first_rows] == [denied_export["id"]]
    assert (
        database.claim_due_jira_worklog_exports(datetime.now(UTC), second_claim) == []
    )
    assert database.mark_jira_worklog_export_failed(
        denied_export["id"],
        first_claim,
        datetime.now(UTC),
        "permission_denied",
        30,
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE jira_worklog_exports SET next_attempt_at=%s WHERE id=%s",
            (datetime.now(UTC), denied_export["id"]),
        )
    contract.deny_worklogs = False
    assert service.sync_due_worklogs() == 1
    assert contract.worklogs["ENG-5"][0]["timeSpentSeconds"] == 600
    service.close()


def test_jira_worklog_sync_modes_release_only_their_eligible_utc_days(
    tmp_path, postgres_url, database
):
    contract = JiraContract()
    settings, _vault, service = _service(tmp_path, postgres_url, database, contract)
    owner = database.bootstrap_admin(settings.admin_email, "hash")
    values, expires_at = service.exchange_code("authorization-code-value")
    integration_id = database.upsert_jira_site(
        "cloud-123", "Acme Jira", "https://acme.atlassian.net", owner["id"]
    )
    service.store_authorization(integration_id, values, expires_at)
    identity = service.current_user("cloud-123", values["access_token"])
    database.upsert_jira_user_connection(
        integration_id,
        owner["id"],
        identity["account_id"],
        identity["display_name"],
        "site",
    )
    project = database.create_project("Jira modes", "", owner["id"])
    database.set_jira_project_mapping(
        integration_id, "10001", "ENG", "Engineering", project["id"]
    )
    service.sync_now(integration_id)
    now = datetime.now(UTC)
    with database.connect() as connection:
        task_id = connection.execute(
            """SELECT id FROM tasks WHERE integration_id=%s
               AND external_display_key='ENG-1'""",
            (integration_id,),
        ).fetchone()["id"]

        def enqueue(work_date) -> None:
            connection.execute(
                """INSERT INTO jira_worklog_dirty_days(
                       integration_id,user_id,task_id,work_date,changed_at
                   ) VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT(integration_id,user_id,task_id,work_date)
                   DO UPDATE SET changed_at=EXCLUDED.changed_at""",
                (integration_id, owner["id"], task_id, work_date, now),
            )

        enqueue(now.date())
        enqueue(now.date() - timedelta(days=1))
        enqueue(now.date() - timedelta(days=2))

    database.set_jira_user_sync_mode(integration_id, owner["id"], "delayed")
    observed = now + timedelta(seconds=1)
    assert database.stage_due_jira_worklogs(observed) == 0
    with database.connect() as connection:
        remaining = {
            row["work_date"]
            for row in connection.execute(
                """SELECT work_date FROM jira_worklog_dirty_days
                   WHERE integration_id=%s AND user_id=%s""",
                (integration_id, owner["id"]),
            ).fetchall()
        }
    assert remaining == {now.date(), now.date() - timedelta(days=1)}

    database.set_jira_user_sync_mode(integration_id, owner["id"], "daily")
    database.stage_due_jira_worklogs(observed)
    with database.connect() as connection:
        remaining = {
            row["work_date"]
            for row in connection.execute(
                """SELECT work_date FROM jira_worklog_dirty_days
                   WHERE integration_id=%s AND user_id=%s""",
                (integration_id, owner["id"]),
            ).fetchall()
        }
    assert remaining == {now.date()}

    database.set_jira_user_sync_mode(integration_id, owner["id"], "off")
    database.stage_due_jira_worklogs(observed)
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) count FROM jira_worklog_dirty_days"
            ).fetchone()["count"]
            == 1
        )

    database.set_jira_user_sync_mode(integration_id, owner["id"], "hourly")
    database.stage_due_jira_worklogs(observed)
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) count FROM jira_worklog_dirty_days"
            ).fetchone()["count"]
            == 0
        )
    service.close()


def test_jira_worklog_aggregation_excludes_time_while_member_was_disconnected(
    tmp_path, postgres_url, database
):
    contract = JiraContract()
    settings, _vault, service = _service(tmp_path, postgres_url, database, contract)
    owner = database.bootstrap_admin(settings.admin_email, "hash")
    site_values, site_expiry = service.exchange_code("authorization-code-value")
    integration_id = database.upsert_jira_site(
        "cloud-123", "Acme Jira", "https://acme.atlassian.net", owner["id"]
    )
    service.store_authorization(integration_id, site_values, site_expiry)
    owner_identity = service.current_user("cloud-123", site_values["access_token"])
    database.upsert_jira_user_connection(
        integration_id,
        owner["id"],
        owner_identity["account_id"],
        owner_identity["display_name"],
        "site",
    )
    project = database.create_project("Jira disconnect window", "", owner["id"])
    database.set_jira_project_mapping(
        integration_id, "10001", "ENG", "Engineering", project["id"]
    )
    service.sync_now(integration_id)
    with database.connect() as connection:
        task_id = connection.execute(
            """SELECT id FROM tasks WHERE integration_id=%s
               AND external_display_key='ENG-1'""",
            (integration_id,),
        ).fetchone()["id"]

    _, invitation = database.create_invitation(
        "jira-gap-member@example.test", owner["id"], 24
    )
    member = database.accept_invitation(
        invitation, hash_password("member password long enough")
    )
    database.add_project_member(project["id"], member["id"], "worker")
    contract.identity_account_id = "712020:gap-member"
    contract.identity_display_name = "Gap Member"
    member_values, member_expiry = service.exchange_code("authorization-code-value")
    service.store_user_authorization(
        integration_id, member["id"], member_values, member_expiry
    )
    database.upsert_jira_user_connection(
        integration_id,
        member["id"],
        contract.identity_account_id,
        contract.identity_display_name,
        "member",
    )
    work_day = datetime.now(UTC).date() - timedelta(days=1)
    day_start = datetime.combine(work_day, datetime.min.time(), tzinfo=UTC)
    with database.connect() as connection:
        connection.execute(
            """UPDATE jira_user_authorization_periods SET started_at=%s
               WHERE integration_id=%s AND user_id=%s AND ended_at IS NULL""",
            (day_start + timedelta(hours=9), integration_id, member["id"]),
        )
    database.disconnect_jira_user(integration_id, member["id"])
    with database.connect() as connection:
        connection.execute(
            """UPDATE jira_user_authorization_periods SET ended_at=%s
               WHERE integration_id=%s AND user_id=%s""",
            (day_start + timedelta(hours=10), integration_id, member["id"]),
        )

    service.store_user_authorization(
        integration_id, member["id"], member_values, member_expiry
    )
    database.upsert_jira_user_connection(
        integration_id,
        member["id"],
        contract.identity_account_id,
        contract.identity_display_name,
        "member",
    )
    with database.connect() as connection:
        connection.execute(
            """UPDATE jira_user_authorization_periods SET started_at=%s
               WHERE integration_id=%s AND user_id=%s AND ended_at IS NULL""",
            (day_start + timedelta(hours=11), integration_id, member["id"]),
        )
        connection.execute(
            """UPDATE jira_user_connections SET next_worklog_sync_at=%s
               WHERE integration_id=%s AND user_id=%s""",
            (datetime.now(UTC), integration_id, member["id"]),
        )
    database.add_manual_time(
        member["id"],
        project["id"],
        task_id,
        day_start + timedelta(hours=9),
        day_start + timedelta(hours=12),
        "Authorization-window test",
        auto_approve=True,
    )
    assert service.sync_due_worklogs() == 1
    assert contract.worklogs["ENG-1"][0]["timeSpentSeconds"] == 7200
    worklog_request = next(
        request
        for request in reversed(contract.requests)
        if request.method == "POST" and request.url.path.endswith("/worklog")
    )
    assert worklog_request.headers["Authorization"] == (
        "Bearer member-access-token-value-long-enough"
    )
    database.set_user_enabled(member["id"], False)
    assert (
        database.get_user_integration_credentials(integration_id, member["id"], "jira")
        is None
    )
    assert database.jira_user_connection(integration_id, member["id"]) is None
    with database.connect() as connection:
        assert (
            connection.execute(
                """SELECT COUNT(*) count FROM jira_user_authorization_periods
               WHERE integration_id=%s AND user_id=%s AND ended_at IS NULL""",
                (integration_id, member["id"]),
            ).fetchone()["count"]
            == 0
        )
    service.close()


def test_jira_rejects_bad_oauth_resources_pagination_and_rate_limits(
    tmp_path, postgres_url, database
):
    settings = _settings(tmp_path, postgres_url)
    vault = IntegrationCredentialVault(
        database, CredentialKeyring.parse(settings.integration_encryption_keys)
    )

    responses = iter(
        [
            httpx.Response(
                200,
                json=[
                    {
                        "id": "cloud-1",
                        "name": "Unsafe",
                        "url": "https://user:password@jira.example.test",
                        "scopes": ["read:jira-work"],
                    }
                ],
            ),
            httpx.Response(429, headers={"Retry-After": "120"}),
        ]
    )
    service = JiraCloudService(
        settings,
        database,
        vault,
        transport=httpx.MockTransport(lambda _request: next(responses)),
        sleep=lambda _seconds: None,
    )
    with pytest.raises(JiraIntegrationError) as invalid:
        service.accessible_resources("a" * 30)
    assert invalid.value.error_code == "invalid_response"
    with pytest.raises(JiraIntegrationError) as limited:
        service.accessible_resources("a" * 30)
    assert limited.value.error_code == "rate_limited"
    assert limited.value.retry_seconds == 120
    service.close()

    oversized = JiraCloudService(
        settings,
        database,
        vault,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"Content-Length": str(8 * 1024 * 1024 + 1)},
                content=b"{}",
            )
        ),
        sleep=lambda _seconds: None,
    )
    with pytest.raises(JiraIntegrationError) as too_large:
        oversized.accessible_resources("a" * 30)
    assert too_large.value.error_code == "invalid_response"
    oversized.close()

    streamed_oversized = JiraCloudService(
        settings,
        database,
        vault,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, stream=_OversizedResponseStream())
        ),
        sleep=lambda _seconds: None,
    )
    with pytest.raises(JiraIntegrationError) as streamed_too_large:
        streamed_oversized.accessible_resources("a" * 30)
    assert streamed_too_large.value.error_code == "invalid_response"
    streamed_oversized.close()


def test_jira_rejects_repeated_issue_page_tokens_and_releases_sync_claim(
    tmp_path, postgres_url, database
):
    settings = _settings(tmp_path, postgres_url)
    vault = IntegrationCredentialVault(
        database, CredentialKeyring.parse(settings.integration_encryption_keys)
    )
    owner = database.bootstrap_admin(settings.admin_email, "hash")
    integration_id = database.upsert_jira_site(
        "cloud-loop", "Loop Jira", "https://loop.atlassian.net", owner["id"]
    )
    project = database.create_project("Loop project", "", owner["id"])
    database.set_jira_project_mapping(
        integration_id, "10001", "ENG", "Engineering", project["id"]
    )
    vault.store(
        integration_id,
        "jira",
        {
            "access_token": "valid-access-token-value-long-enough",
            "refresh_token": "valid-refresh-token-value-long-enough",
            "scope": "offline_access read:jira-user read:jira-work write:jira-work",
        },
        access_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    searches = 0

    def repeated_token(request: httpx.Request) -> httpx.Response:
        nonlocal searches
        if request.url.path.endswith("/rest/api/3/project/search"):
            return httpx.Response(
                200,
                json={
                    "total": 1,
                    "isLast": True,
                    "values": [{"id": "10001", "key": "ENG", "name": "Engineering"}],
                },
            )
        if request.url.path.endswith("/rest/api/3/search/jql"):
            searches += 1
            return httpx.Response(200, json={"issues": [], "nextPageToken": "loop"})
        raise AssertionError(f"Unexpected Jira request: {request.method} {request.url}")

    service = JiraCloudService(
        settings,
        database,
        vault,
        transport=httpx.MockTransport(repeated_token),
        sleep=lambda _seconds: None,
    )
    with pytest.raises(JiraIntegrationError) as failed:
        service.sync_now(integration_id)
    assert failed.value.error_code == "invalid_response"
    assert searches == 2
    integration = database.get_jira_integration(integration_id)
    assert integration["last_error_code"] == "invalid_response"
    assert integration["sync_claim_token"] is None
    service.close()


def test_jira_invalid_rotating_grant_fails_closed_and_releases_claim(
    tmp_path, postgres_url, database
):
    settings = _settings(tmp_path, postgres_url)
    vault = IntegrationCredentialVault(
        database, CredentialKeyring.parse(settings.integration_encryption_keys)
    )
    owner = database.bootstrap_admin(settings.admin_email, "hash")
    integration_id = database.upsert_jira_site(
        "cloud-expired", "Expired Jira", "https://expired.atlassian.net", owner["id"]
    )
    vault.store(
        integration_id,
        "jira",
        {
            "access_token": "expired-access-token-value-long-enough",
            "refresh_token": "expired-refresh-token-value-long-enough",
            "scope": "offline_access read:jira-user read:jira-work write:jira-work",
        },
        access_expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    service = JiraCloudService(
        settings,
        database,
        vault,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                400,
                json={
                    "error": "invalid_grant",
                    "error_description": "sensitive provider text is ignored",
                },
            )
        ),
        sleep=lambda _seconds: None,
    )
    with pytest.raises(JiraIntegrationError) as expired:
        service.access_token(integration_id)
    assert expired.value.error_code == "reauthorization_required"
    row = database.get_integration_credentials(integration_id, "jira")
    assert row is not None and row["refresh_claim_token"] is None
    assert "sensitive" not in str(expired.value)
    service.close()


def test_jira_full_reconciliation_requires_two_misses_and_reconnect_resets_cursors(
    database,
):
    owner = database.bootstrap_admin("owner@example.test", "hash")
    integration_id = database.upsert_jira_site(
        "cloud-reconcile",
        "Reconciliation Jira",
        "https://reconcile.atlassian.net",
        owner["id"],
    )
    project = database.create_project("Reconciliation", "", owner["id"])
    database.set_jira_project_mapping(
        integration_id, "10001", "ENG", "Engineering", project["id"]
    )
    observed_at = datetime.now(UTC)
    task_id = database.apply_jira_issue(
        integration_id,
        "10001",
        {
            "id": "20001",
            "key": "ENG-1",
            "summary": "Eventually visible",
            "description": "",
            "done": False,
            "url": "https://reconcile.atlassian.net/browse/ENG-1",
            "updated_at": observed_at,
        },
        observed_at,
    )
    first_snapshot = database.integration_sync_snapshot_at()
    assert (
        database.archive_missing_jira_issues(
            integration_id, "10001", [], first_snapshot
        )
        == 0
    )
    assert database.get_task(task_id)["status"] == "active"
    assert (
        database.archive_missing_jira_issues(
            integration_id, "10001", [], first_snapshot + timedelta(seconds=1)
        )
        == 1
    )
    assert database.get_task(task_id)["status"] == "archived"

    database.mark_jira_mapping_synced(integration_id, "10001", observed_at, full=True)
    assert database.jira_project_mappings(integration_id)[0]["last_full_sync_at"]
    assert (
        database.upsert_jira_site(
            "cloud-reconcile",
            "Reconciliation Jira",
            "https://reconcile.atlassian.net",
            owner["id"],
        )
        == integration_id
    )
    mapping = database.jira_project_mappings(integration_id)[0]
    assert mapping["last_full_sync_at"] is None
    assert mapping["last_incremental_sync_at"] is None


def test_jira_reconciliation_ignores_items_observed_after_its_snapshot(database):
    owner = database.bootstrap_admin("snapshot-owner@example.test", "hash")
    integration_id = database.upsert_jira_site(
        "cloud-snapshot",
        "Snapshot Jira",
        "https://snapshot.atlassian.net",
        owner["id"],
    )
    project = database.create_project("Snapshot project", "", owner["id"])
    database.set_jira_project_mapping(
        integration_id, "10001", "ENG", "Engineering", project["id"]
    )
    stale_snapshot = database.integration_sync_snapshot_at()
    observed_at = datetime.now(UTC)
    task_id = database.apply_jira_issue(
        integration_id,
        "10001",
        {
            "id": "20001",
            "key": "ENG-1",
            "summary": "Arrived during reconciliation",
            "description": "",
            "done": False,
            "url": "https://snapshot.atlassian.net/browse/ENG-1",
            "updated_at": observed_at,
        },
        observed_at,
    )

    assert (
        database.archive_missing_jira_issues(
            integration_id, "10001", [], stale_snapshot
        )
        == 0
    )
    with database.connect() as connection:
        row = connection.execute(
            "SELECT status,external_missing_since FROM tasks WHERE id=%s", (task_id,)
        ).fetchone()
    assert row["status"] == "active"
    assert row["external_missing_since"] is None

    first_real_miss = database.integration_sync_snapshot_at()
    assert (
        database.archive_missing_jira_issues(
            integration_id, "10001", [], first_real_miss
        )
        == 0
    )
    second_real_miss = database.integration_sync_snapshot_at()
    assert (
        database.archive_missing_jira_issues(
            integration_id, "10001", [], second_real_miss
        )
        == 1
    )
    assert database.get_task(task_id)["status"] == "archived"


def test_jira_project_access_loss_archives_tasks_and_stops_live_sessions(
    tmp_path, postgres_url, database
):
    settings = _settings(tmp_path, postgres_url)
    vault = IntegrationCredentialVault(
        database, CredentialKeyring.parse(settings.integration_encryption_keys)
    )
    contract = JiraContract()
    project_access = True

    def provider(request: httpx.Request) -> httpx.Response:
        if not project_access and request.url.path.endswith(
            "/rest/api/3/project/search"
        ):
            return httpx.Response(
                200,
                json={"total": 0, "isLast": True, "values": []},
            )
        return contract(request)

    service = JiraCloudService(
        settings,
        database,
        vault,
        transport=httpx.MockTransport(provider),
        sleep=lambda _seconds: None,
    )
    owner = database.bootstrap_admin(settings.admin_email, "hash")
    integration_id = database.upsert_jira_site(
        "cloud-123", "Acme Jira", "https://acme.atlassian.net", owner["id"]
    )
    project = database.create_project("Access-loss project", "", owner["id"])
    database.set_jira_project_mapping(
        integration_id, "10001", "ENG", "Engineering", project["id"]
    )
    vault.store(
        integration_id,
        "jira",
        {
            "access_token": "valid-access-token-value-long-enough",
            "refresh_token": "valid-refresh-token-value-long-enough",
            "scope": "offline_access read:jira-user read:jira-work write:jira-work",
        },
        access_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert service.sync_now(integration_id) == 2
    with database.connect() as connection:
        task = connection.execute(
            "SELECT * FROM tasks WHERE integration_id=%s AND status='active'",
            (integration_id,),
        ).fetchone()
    device, _ = database.create_device("Access-loss device", owner["id"], project["id"])
    session = database.sync_work_session(device, "active", task["id"], project["id"])

    project_access = False
    assert service.sync_now(integration_id) == 0
    assert database.jira_project_mappings(integration_id) == []
    assert database.get_task(task["id"])["status"] == "archived"
    assert database.get_work_session(session["id"])["status"] == "stopped"
    service.close()


def test_owner_jira_web_flow_is_state_bound_scoped_and_removes_credentials(
    tmp_path, postgres_url
):
    contract = JiraContract()
    settings = _settings(tmp_path, postgres_url)
    app = create_app(settings, jira_transport=httpx.MockTransport(contract))
    with TestClient(app) as client:
        database = app.state.database
        _login(client, settings.admin_email, settings.admin_password)
        settings_page = client.get("/settings")
        assert settings_page.status_code == 200
        assert "Connect Jira Cloud" in settings_page.text
        assert settings.jira_client_secret not in settings_page.text
        assert settings.integration_encryption_keys not in settings_page.text

        connect = client.get("/integrations/jira/connect", follow_redirects=False)
        assert connect.status_code == 303
        state = parse_qs(urlparse(connect.headers["location"]).query)["state"][0]
        assert (
            client.get(
                "/integrations/jira/callback",
                params={"code": "authorization-code-value", "state": "wrong"},
            ).status_code
            == 403
        )

        connect = client.get("/integrations/jira/connect", follow_redirects=False)
        state = parse_qs(urlparse(connect.headers["location"]).query)["state"][0]
        callback = client.get(
            "/integrations/jira/callback",
            params={"code": "authorization-code-value", "state": state},
            follow_redirects=False,
        )
        assert callback.status_code == 303, callback.text
        integration_id = callback.headers["location"].rsplit("/", 1)[-1]
        integration_page = client.get(callback.headers["location"])
        assert integration_page.status_code == 200
        assert "ENG · Engineering" in integration_page.text
        assert "initial-access-token" not in integration_page.text

        owner = database.get_user_by_email(settings.admin_email)
        project = database.create_project("Web-mapped project", "", owner["id"])
        mapped = client.post(
            f"/integrations/jira/{integration_id}/mappings",
            data={
                "csrf": _csrf(integration_page),
                "external_project_id": "10001",
                "project_id": project["id"],
            },
            follow_redirects=False,
        )
        assert mapped.status_code == 303, mapped.text
        page = client.get(mapped.headers["location"])
        synced = client.post(
            f"/integrations/jira/{integration_id}/sync",
            data={"csrf": _csrf(page)},
            follow_redirects=False,
        )
        assert synced.status_code == 303, synced.text
        assert database.get_jira_integration(integration_id)["synced_task_count"] == 2
        assert "Jira managed" in client.get(f"/projects/{project['id']}").text
        owner_connection = database.jira_user_connection(integration_id, owner["id"])
        assert owner_connection["credential_kind"] == "site"
        assert app.state.jira.user_access_token(integration_id, owner["id"]).startswith(
            "initial-access"
        )

        _, invitation = database.create_invitation(
            "jira-member@example.test", owner["id"], 24
        )
        member = database.accept_invitation(
            invitation, hash_password("member password long enough")
        )
        database.add_project_member(project["id"], member["id"], "worker")
        client.cookies.clear()
        _login(client, member["email"], "member password long enough")
        member_project = client.get(f"/projects/{project['id']}")
        assert f"/integrations/jira/{integration_id}/account" in member_project.text
        account_page = client.get(f"/integrations/jira/{integration_id}/account")
        assert "Connect your Jira account" in account_page.text

        contract.identity_account_id = "712020:member-atlassian-account"
        contract.identity_display_name = "Jira Member"
        member_connect = client.get(
            f"/integrations/jira/{integration_id}/connect-account",
            follow_redirects=False,
        )
        member_state = parse_qs(urlparse(member_connect.headers["location"]).query)[
            "state"
        ][0]
        member_callback = client.get(
            "/integrations/jira/callback",
            params={"code": "authorization-code-value", "state": member_state},
            follow_redirects=False,
        )
        assert member_callback.status_code == 303
        member_connection = database.jira_user_connection(integration_id, member["id"])
        assert member_connection["credential_kind"] == "member"
        assert member_connection["display_name"] == "Jira Member"
        assert (
            database.get_user_integration_credentials(
                integration_id, member["id"], "jira"
            )
            is not None
        )
        assert app.state.jira.user_access_token(
            integration_id, member["id"]
        ).startswith("member-access")
        connected_page = client.get(member_callback.headers["location"])
        changed_mode = client.post(
            f"/integrations/jira/{integration_id}/account/settings",
            data={"csrf": _csrf(connected_page), "time_sync_mode": "hourly"},
            follow_redirects=False,
        )
        assert changed_mode.status_code == 303
        assert (
            database.jira_user_connection(integration_id, member["id"])[
                "time_sync_mode"
            ]
            == "hourly"
        )

        client.cookies.clear()
        _login(client, settings.admin_email, settings.admin_password)
        contract.identity_account_id = "712020:owner-atlassian-account"
        contract.identity_display_name = "Dayfinch Owner"

        with database.connect() as connection:
            active_task = connection.execute(
                "SELECT * FROM tasks WHERE integration_id=%s AND status='active'",
                (integration_id,),
            ).fetchone()
        device, _ = database.create_device(
            "Jira disconnect device", owner["id"], project["id"]
        )
        session = database.sync_work_session(
            device, "active", active_task["id"], project["id"]
        )

        page = client.get(f"/integrations/jira/{integration_id}")
        disconnected = client.post(
            f"/integrations/jira/{integration_id}/disconnect",
            data={"csrf": _csrf(page)},
            follow_redirects=False,
        )
        assert disconnected.status_code == 303
        assert database.get_jira_integration(integration_id)["enabled"] is False
        assert database.get_integration_credentials(integration_id, "jira") is None
        assert (
            database.get_user_integration_credentials(
                integration_id, member["id"], "jira"
            )
            is None
        )
        assert database.list_jira_user_connections(integration_id) == []
        retained = database.get_task(active_task["id"])
        assert retained["status"] == "active"
        assert retained["external_read_only"] is False
        assert database.get_work_session(session["id"])["status"] == "active"
        assert "Jira managed" not in client.get(f"/projects/{project['id']}").text

        reconnect = client.get("/integrations/jira/connect", follow_redirects=False)
        reconnect_state = parse_qs(urlparse(reconnect.headers["location"]).query)[
            "state"
        ][0]
        reconnected = client.get(
            "/integrations/jira/callback",
            params={"code": "authorization-code-value", "state": reconnect_state},
            follow_redirects=False,
        )
        assert reconnected.status_code == 303
        assert reconnected.headers["location"].endswith(integration_id)
        resynced = client.post(
            f"/integrations/jira/{integration_id}/sync",
            data={"csrf": _csrf(client.get(reconnected.headers["location"]))},
            follow_redirects=False,
        )
        assert resynced.status_code == 303
        reattached = database.get_task(active_task["id"])
        assert reattached["external_read_only"] is True
        assert database.get_work_session(session["id"])["status"] == "active"

        with database.connect() as connection:
            details = [
                row["details"]
                for row in connection.execute(
                    "SELECT details FROM audit_events WHERE action LIKE 'jira.%'"
                ).fetchall()
            ]
        assert all("token" not in detail.lower() for detail in details)


def test_jira_callback_rejects_ambiguous_legacy_account_grant(tmp_path, postgres_url):
    contract = JiraContract()

    def multiple_resources(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token/accessible-resources":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "cloud-1",
                        "name": "First Jira",
                        "url": "https://first.atlassian.net",
                        "scopes": [
                            "offline_access",
                            "read:jira-user",
                            "read:jira-work",
                            "write:jira-work",
                        ],
                    },
                    {
                        "id": "cloud-2",
                        "name": "Second Jira",
                        "url": "https://second.atlassian.net",
                        "scopes": [
                            "offline_access",
                            "read:jira-user",
                            "read:jira-work",
                            "write:jira-work",
                        ],
                    },
                ],
            )
        return contract(request)

    settings = _settings(tmp_path, postgres_url)
    app = create_app(settings, jira_transport=httpx.MockTransport(multiple_resources))
    with TestClient(app) as client:
        _login(client, settings.admin_email, settings.admin_password)
        connect = client.get("/integrations/jira/connect", follow_redirects=False)
        state = parse_qs(urlparse(connect.headers["location"]).query)["state"][0]
        callback = client.get(
            "/integrations/jira/callback",
            params={"code": "authorization-code-value", "state": state},
        )
        assert callback.status_code == 422
        assert "resource-restricted" in callback.text
        assert app.state.database.list_jira_integrations() == []


def test_jira_sync_claims_are_exclusive_across_replicas(database):
    owner = database.bootstrap_admin("owner@example.test", "hash")
    first = database.upsert_jira_site(
        "cloud-one", "One", "https://one.atlassian.net", owner["id"]
    )
    second = database.upsert_jira_site(
        "cloud-two", "Two", "https://two.atlassian.net", owner["id"]
    )
    observed_at = datetime.now(UTC) + timedelta(seconds=1)
    claimed_one = database.claim_due_jira_integrations(
        observed_at, "11111111-1111-1111-1111-111111111111", limit=1
    )
    claimed_two = database.claim_due_jira_integrations(
        observed_at, "22222222-2222-2222-2222-222222222222", limit=1
    )
    assert {claimed_one[0]["id"], claimed_two[0]["id"]} == {first, second}
    assert (
        database.claim_due_jira_integrations(
            observed_at, "33333333-3333-3333-3333-333333333333"
        )
        == []
    )


def test_jira_configuration_requires_encrypted_complete_oauth(tmp_path):
    base = dict(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024,
        retention_days=30,
    )
    with pytest.raises(RuntimeError, match="configured together"):
        Settings(**base, jira_client_id="configured-alone").prepare()
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEYS"):
        Settings(
            **base,
            jira_client_id="client",
            jira_client_secret="secret",
        ).prepare()
    key_spec = _keyring_spec()
    settings = Settings(
        **{**base, "cookie_secure": True},
        environment="production",
        admin_email="owner@example.test",
        public_url="https://tracker.example.test",
        allowed_hosts=("tracker.example.test",),
        integration_encryption_keys=key_spec,
        jira_client_id="client",
        jira_client_secret="secret",
        jira_api_url="http://api.jira.test",
    )
    settings.prepare()
    with pytest.raises(RuntimeError, match="Jira.*HTTPS"):
        settings.validate_for_nonlocal()

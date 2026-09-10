from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from api.config import Settings
from api.main import create_app
from api.security import hash_password
from api.services.integration_credentials import (
    CredentialKeyring,
    IntegrationCredentialError,
    IntegrationCredentialVault,
)
from api.services.slack_integration import (
    SLACK_SCOPES,
    SlackCloudService,
    SlackIntegrationError,
)


def _keyring_spec() -> str:
    return "primary:" + base64.urlsafe_b64encode(b"s" * 32).decode().rstrip("=")


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
        slack_client_id="slack-client-id",
        slack_client_secret="slack-client-secret-value",
        slack_authorize_url="https://slack.test/oauth/v2/authorize",
        slack_token_url="https://slack.test/api/oauth.v2.access",
        slack_api_url="https://slack.test/api",
    )
    settings.prepare()
    return settings


class SlackContract:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.refreshes = 0
        self.revocations = 0
        self.repeat_channel_cursor = False
        self.rate_limit_channels = False
        self.permission_denied_channels = False
        self.oversized_oauth = False
        self.invalid_message_timestamp = False
        self.lose_next_message_response = False
        self.messages: dict[str, dict[str, str]] = {}

    @staticmethod
    def _oauth_payload(access_token: str, refresh_token: str) -> dict:
        return {
            "ok": True,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": 43_200,
            "token_type": "bot",
            "scope": ",".join(sorted(SLACK_SCOPES)),
            "bot_user_id": "B123BOT",
            "team": {"id": "T123TEAM", "name": "Acme Slack"},
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/oauth.v2.access":
            if self.oversized_oauth:
                return httpx.Response(200, content=b"x" * (4 * 1024 * 1024 + 1))
            body = parse_qs(request.content.decode())
            if body.get("grant_type") == ["refresh_token"]:
                self.refreshes += 1
                return httpx.Response(
                    200,
                    json=self._oauth_payload(
                        "xoxe.xoxb-refreshed-access", "xoxe-refreshed-refresh"
                    ),
                )
            assert body["code"] == ["authorization-code"]
            return httpx.Response(
                200,
                json=self._oauth_payload(
                    "xoxe.xoxb-initial-access", "xoxe-initial-refresh"
                ),
            )
        if request.url.path == "/api/conversations.list":
            if self.permission_denied_channels:
                return httpx.Response(200, json={"ok": False, "error": "missing_scope"})
            if self.rate_limit_channels:
                return httpx.Response(429, headers={"Retry-After": "17"}, json={})
            if request.url.params.get("cursor") == "channel-page-2":
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "channels": [
                            {
                                "id": "G222PRIVATE",
                                "name": "private-team",
                                "is_archived": False,
                            }
                        ],
                        "response_metadata": {
                            "next_cursor": "channel-page-2"
                            if self.repeat_channel_cursor
                            else ""
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "channels": [
                        {"id": "C111GENERAL", "name": "general", "is_archived": False}
                    ],
                    "response_metadata": {"next_cursor": "channel-page-2"},
                },
            )
        if request.url.path == "/api/users.list":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "members": [
                        {
                            "id": "U111ALICE",
                            "name": "alice",
                            "deleted": False,
                            "is_bot": False,
                            "is_app_user": False,
                            "profile": {
                                "display_name": "Alice",
                                "real_name": "Alice A",
                            },
                        },
                        {
                            "id": "B123BOT",
                            "name": "dayfinch",
                            "deleted": False,
                            "is_bot": True,
                            "profile": {},
                        },
                    ],
                    "response_metadata": {"next_cursor": ""},
                },
            )
        if request.url.path == "/api/chat.postMessage":
            body = json.loads(request.content)
            message_id = body["client_msg_id"]
            message = self.messages.setdefault(
                message_id,
                {
                    "channel": body["channel"],
                    "text": body["text"],
                    "ts": f"1700000000.{len(self.messages) + 1:06d}",
                },
            )
            if self.lose_next_message_response:
                self.lose_next_message_response = False
                raise httpx.ReadError("Slack response lost", request=request)
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "channel": message["channel"],
                    "ts": "invalid"
                    if self.invalid_message_timestamp
                    else message["ts"],
                },
            )
        if request.url.path == "/api/auth.revoke":
            self.revocations += 1
            return httpx.Response(200, json={"ok": True, "revoked": True})
        raise AssertionError(
            f"Unexpected Slack request: {request.method} {request.url}"
        )


def _service(tmp_path, postgres_url, database, contract: SlackContract):
    settings = _settings(tmp_path, postgres_url)
    vault = IntegrationCredentialVault(
        database, CredentialKeyring.parse(settings.integration_encryption_keys)
    )
    service = SlackCloudService(
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


def _login(client: TestClient, settings: Settings, email="", password="") -> None:
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


def test_slack_oauth_discovery_rotation_and_pagination_guards(
    tmp_path, postgres_url, database, monkeypatch
):
    contract = SlackContract()
    settings, vault, service = _service(tmp_path, postgres_url, database, contract)
    owner = database.bootstrap_admin(
        settings.admin_email, hash_password("test-password-long")
    )
    authorization = httpx.URL(service.authorization_url("s" * 48))
    assert authorization.params["state"] == "s" * 48
    assert set(authorization.params["scope"].split(",")) == SLACK_SCOPES

    values, expires_at = service.exchange_code("authorization-code")
    integration_id = service.complete_authorization(owner["id"], values, expires_at)
    row = database.get_integration_credentials(integration_id, "slack")
    assert row is not None
    assert b"xoxe.xoxb-initial-access" not in bytes(row["ciphertext"])
    assert vault.open(integration_id, "slack").values["team_id"] == "T123TEAM"

    destinations = service.list_destinations(integration_id)
    assert {(item["id"], item["name"]) for item in destinations} == {
        ("C111GENERAL", "#general"),
        ("G222PRIVATE", "#private-team"),
        ("U111ALICE", "@Alice"),
    }
    contract.rate_limit_channels = True
    with pytest.raises(SlackIntegrationError) as rate_limit:
        service.list_destinations(integration_id)
    assert rate_limit.value.error_code == "rate_limited"
    assert rate_limit.value.retry_seconds == 17
    contract.rate_limit_channels = False
    contract.repeat_channel_cursor = True
    with pytest.raises(SlackIntegrationError) as repeated:
        service.list_destinations(integration_id)
    assert repeated.value.error_code == "invalid_response"
    contract.repeat_channel_cursor = False
    contract.permission_denied_channels = True
    with pytest.raises(SlackIntegrationError) as permission_denied:
        service.list_destinations(integration_id)
    assert permission_denied.value.error_code == "permission_denied"
    contract.permission_denied_channels = False

    oversized_contract = SlackContract()
    oversized_contract.oversized_oauth = True
    oversized_service = SlackCloudService(
        settings,
        database,
        vault,
        transport=httpx.MockTransport(oversized_contract),
        sleep=lambda _seconds: None,
    )
    with pytest.raises(SlackIntegrationError) as oversized:
        oversized_service.exchange_code("authorization-code")
    assert oversized.value.error_code == "invalid_response"
    assert settings.slack_client_secret not in str(oversized.value)
    oversized_service.close()

    database.expire_integration_access(integration_id, "slack", datetime.now(UTC))
    assert service.access_token(integration_id) == "xoxe.xoxb-refreshed-access"
    assert contract.refreshes == 1
    service.add_destination(integration_id, "C111GENERAL", "channel")
    project = database.create_project("Invalid Slack response", "", owner["id"])
    task = database.create_task(project["id"], "Test Slack response", "", owner["id"])
    device, _token = database.create_device(
        "Invalid Slack response device", owner["id"], project["id"]
    )
    database.sync_work_session(device, "active", task["id"], project["id"])
    contract.invalid_message_timestamp = True
    assert service.deliver_due() == 0
    with database.connect() as connection:
        failed_message = connection.execute(
            "SELECT id,last_error_code FROM slack_outbox WHERE sent_at IS NULL"
        ).fetchone()
        assert failed_message["last_error_code"] == "invalid_response"
        connection.execute(
            "UPDATE slack_outbox SET attempt_count=24,next_attempt_at=%s WHERE id=%s",
            (datetime.now(UTC), failed_message["id"]),
        )
    terminal_claim = "33333333-3333-4333-8333-333333333333"
    assert (
        database.claim_due_slack_messages(datetime.now(UTC), terminal_claim)[0]["id"]
        == failed_message["id"]
    )
    assert database.mark_slack_message_failed(
        failed_message["id"],
        terminal_claim,
        datetime.now(UTC),
        "invalid_response",
        300,
    )
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT discarded_at FROM slack_outbox WHERE id=%s",
                (failed_message["id"],),
            ).fetchone()["discarded_at"]
            is not None
        )
    assert (
        database.claim_due_slack_messages(
            datetime.now(UTC) + timedelta(days=1), str(uuid4())
        )
        == []
    )
    contract.invalid_message_timestamp = False
    service.revoke_stored(integration_id)
    database.disconnect_slack_integration(integration_id)
    assert contract.revocations == 1
    assert database.get_integration_credentials(integration_id, "slack") is None
    monkeypatch.setattr(
        vault,
        "store",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            IntegrationCredentialError("simulated storage failure")
        ),
    )
    with pytest.raises(SlackIntegrationError) as failed_reconnect:
        service.complete_authorization(owner["id"], values, expires_at)
    assert failed_reconnect.value.error_code == "credential_storage_failed"
    assert database.get_slack_integration(integration_id)["enabled"] is False
    assert database.get_integration_credentials(integration_id, "slack") is None
    service.close()


def test_slack_outbox_rules_lost_response_recovery_and_replica_claims(
    tmp_path, postgres_url, database
):
    contract = SlackContract()
    settings, _vault, service = _service(tmp_path, postgres_url, database, contract)
    owner = database.bootstrap_admin(
        settings.admin_email, hash_password("test-password-long")
    )
    values, expires_at = service.exchange_code("authorization-code")
    integration_id = service.complete_authorization(owner["id"], values, expires_at)
    service.add_destination(integration_id, "C111GENERAL", "channel")

    project = database.create_project("Slack project", "", owner["id"])
    task = database.create_task(project["id"], "Slack task", "", owner["id"])
    device, _token = database.create_device(
        "Slack test device", owner["id"], project["id"]
    )
    started_at = datetime.now(UTC) - timedelta(hours=1)
    database.sync_work_session(
        device,
        "active",
        task["id"],
        project["id"],
        observed_at=started_at,
    )
    database.sync_work_session(
        device,
        "paused",
        task["id"],
        project["id"],
        observed_at=started_at + timedelta(minutes=30),
    )
    database.sync_work_session(
        device,
        "stopped",
        task["id"],
        project["id"],
        observed_at=started_at + timedelta(minutes=30),
    )
    todo_id = database.create_global_todo(
        "Review release", "", [project["id"]], False, owner["id"]
    )
    database.set_todo_complete(todo_id, project["id"], owner["id"], True)
    with database.connect() as connection:
        queued = connection.execute(
            "SELECT event_type FROM slack_outbox ORDER BY created_at,event_type"
        ).fetchall()
    assert {row["event_type"] for row in queued} == {
        "timer_started",
        "timer_stopped",
        "todo_completed",
    }
    assert len(queued) == 3

    first_claim = "11111111-1111-4111-8111-111111111111"
    second_claim = "22222222-2222-4222-8222-222222222222"
    claimed = database.claim_due_slack_messages(
        datetime.now(UTC), first_claim, limit=25
    )
    assert len(claimed) == 1
    assert (
        database.claim_due_slack_messages(datetime.now(UTC), second_claim, limit=25)
        == []
    )
    assert database.mark_slack_message_failed(
        claimed[0]["id"], first_claim, datetime.now(UTC), "test_release", 1
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE slack_outbox SET next_attempt_at=%s WHERE id=%s",
            (datetime.now(UTC), claimed[0]["id"]),
        )
        connection.execute(
            """UPDATE slack_outbox SET next_attempt_at=%s
               WHERE id<>%s AND sent_at IS NULL""",
            (datetime.now(UTC) + timedelta(hours=1), claimed[0]["id"]),
        )

    contract.lose_next_message_response = True
    assert service.deliver_due() == 0
    assert len(contract.messages) == 1
    with database.connect() as connection:
        failed = connection.execute(
            "SELECT id,last_error_code FROM slack_outbox WHERE last_error_code='network_error'"
        ).fetchone()
        assert failed is not None
        connection.execute(
            "UPDATE slack_outbox SET next_attempt_at=%s WHERE id=%s",
            (datetime.now(UTC), failed["id"]),
        )
    assert service.deliver_due() == 1
    assert len(contract.messages) == 1
    sent_message = next(iter(contract.messages.values()))
    assert "started tracking" in sent_message["text"]
    assert "Slack task" in sent_message["text"]

    with database.connect() as connection:
        connection.execute(
            """UPDATE slack_outbox SET next_attempt_at=%s
               WHERE sent_at IS NULL""",
            (datetime.now(UTC),),
        )

    for expected in ("stopped tracking", "completed Review release"):
        assert service.deliver_due() == 1
        assert any(
            expected in message["text"] for message in contract.messages.values()
        )

    database.set_slack_user_notification_rule(integration_id, owner["id"], False, False)
    second_session = database.sync_work_session(
        device,
        "active",
        task["id"],
        project["id"],
        observed_at=datetime.now(UTC),
    )
    database.sync_work_session(
        device,
        "stopped",
        task["id"],
        project["id"],
        observed_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    with database.connect() as connection:
        second_events = connection.execute(
            "SELECT COUNT(*) count FROM slack_outbox WHERE session_id=%s",
            (second_session["id"],),
        ).fetchone()
    assert second_events["count"] == 0
    database.set_slack_user_notification_rule(integration_id, owner["id"], True, True)
    third_session = database.sync_work_session(
        device,
        "active",
        task["id"],
        project["id"],
        observed_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) count FROM slack_outbox WHERE session_id=%s",
                (third_session["id"],),
            ).fetchone()["count"]
            == 1
        )
    database.set_user_enabled(owner["id"], False)
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) count FROM slack_outbox WHERE user_id=%s AND sent_at IS NULL",
                (owner["id"],),
            ).fetchone()["count"]
            == 0
        )
    service.close()


def test_slack_owner_web_flow_is_state_bound_scoped_and_configurable(
    tmp_path, postgres_url
):
    contract = SlackContract()
    settings = _settings(tmp_path, postgres_url)
    app = create_app(settings, slack_transport=httpx.MockTransport(contract))
    with TestClient(app) as client:
        _login(client, settings)
        settings_page = client.get("/settings")
        assert settings_page.status_code == 200
        assert "Connect Slack" in settings_page.text
        assert settings.slack_client_secret not in settings_page.text
        assert settings.integration_encryption_keys not in settings_page.text

        connect = client.get("/integrations/slack/connect", follow_redirects=False)
        assert connect.status_code == 303
        provider_url = httpx.URL(connect.headers["location"])
        assert set(provider_url.params["scope"].split(",")) == SLACK_SCOPES
        assert (
            client.get(
                "/integrations/slack/callback",
                params={"code": "authorization-code", "state": "wrong"},
            ).status_code
            == 403
        )

        connect = client.get("/integrations/slack/connect", follow_redirects=False)
        provider_url = httpx.URL(connect.headers["location"])
        callback = client.get(
            "/integrations/slack/callback",
            params={
                "code": "authorization-code",
                "state": provider_url.params["state"],
            },
            follow_redirects=False,
        )
        assert callback.status_code == 303, callback.text
        integration_id = callback.headers["location"].rsplit("/", 1)[-1]
        page = client.get(callback.headers["location"])
        assert page.status_code == 200
        assert "#general" in page.text
        assert "@Alice" in page.text
        assert "xoxe.xoxb-initial-access" not in page.text

        added = client.post(
            f"/integrations/slack/{integration_id}/destinations",
            data={"csrf": _csrf(page), "target": "channel:C111GENERAL"},
            follow_redirects=False,
        )
        assert added.status_code == 303, added.text
        page = client.get(added.headers["location"])
        assert "Configured destinations" in page.text
        assert (
            app.state.database.slack_destinations(integration_id)[0]["slack_target_id"]
            == "C111GENERAL"
        )

        defaults = client.post(
            f"/integrations/slack/{integration_id}/defaults",
            data={"csrf": _csrf(page), "todo_events": "on"},
            follow_redirects=False,
        )
        assert defaults.status_code == 303
        integration = app.state.database.get_slack_integration(integration_id)
        assert integration["timer_events"] is False
        assert integration["todo_events"] is True

        owner = app.state.database.get_user_by_email(settings.admin_email)
        page = client.get(f"/integrations/slack/{integration_id}")
        rule = client.post(
            f"/integrations/slack/{integration_id}/users/{owner['id']}",
            data={
                "csrf": _csrf(page),
                "timer_events": "on",
                "todo_events": "off",
            },
            follow_redirects=False,
        )
        assert rule.status_code == 303
        owner_rule = next(
            row
            for row in app.state.database.slack_notification_users(integration_id)
            if row["id"] == owner["id"]
        )
        assert owner_rule["timer_events"] is True
        assert owner_rule["todo_events"] is False

        _, token = app.state.database.create_invitation(
            "slack-member@example.test", owner["id"], 24
        )
        member = app.state.database.accept_invitation(
            token, hash_password("member password long enough")
        )
        _login(client, settings, member["email"], "member password long enough")
        assert client.get(f"/integrations/slack/{integration_id}").status_code == 403
        assert (
            client.post(
                f"/integrations/slack/{integration_id}/defaults",
                data={"csrf": _csrf(client.get("/")), "timer_events": "on"},
            ).status_code
            == 403
        )

        _login(client, settings)
        page = client.get(f"/integrations/slack/{integration_id}")
        disconnected = client.post(
            f"/integrations/slack/{integration_id}/disconnect",
            data={"csrf": _csrf(page)},
            follow_redirects=False,
        )
        assert disconnected.status_code == 303
        assert contract.revocations == 1
        assert (
            app.state.database.get_integration_credentials(integration_id, "slack")
            is None
        )
        assert (
            app.state.database.get_slack_integration(integration_id)["enabled"] is False
        )

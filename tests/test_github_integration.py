from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from api.config import Settings
from api.main import create_app
from api.security import hash_password
from api.services.github_integration import GitHubAppService, GitHubIntegrationError


def _github_key() -> tuple[str, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return base64.b64encode(pem).decode(), key


def _settings(tmp_path, postgres_url: str, private_key_b64: str) -> Settings:
    return Settings(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024 * 1024,
        retention_days=30,
        admin_email="owner@example.test",
        database_url=postgres_url,
        public_url="http://testserver",
        github_app_slug="dayfinch-test",
        github_client_id="Iv1.dayfinchtest",
        github_client_secret="github-client-secret-value",
        github_private_key_b64=private_key_b64,
        github_webhook_secret="github-webhook-secret-value-long-enough",
        github_api_url="https://api.github.test",
        github_web_url="https://github.test",
    )


def _csrf(response) -> str:
    matched = re.search(r'name="csrf" value="([^"]+)', response.text)
    assert matched, response.text
    return matched.group(1)


def _login(client: TestClient, email: str, password: str) -> None:
    client.cookies.clear()
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf": _csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _member(database, owner: dict) -> dict:
    _, token = database.create_invitation("github-member@example.test", owner["id"], 24)
    return database.accept_invitation(
        token, hash_password("member password long enough")
    )


class GitHubContract:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.oauth_revoked = False
        self.issues = [
            {
                "node_id": "I_kw_open",
                "number": 7,
                "title": "Implement synchronization",
                "body": "Acceptance criteria",
                "state": "open",
                "html_url": "https://github.test/acme/widget/issues/7",
                "updated_at": "2026-09-08T10:00:00Z",
            },
            {
                "node_id": "I_kw_closed",
                "number": 8,
                "title": "Old completed work",
                "body": None,
                "state": "closed",
                "html_url": "https://github.test/acme/widget/issues/8",
                "updated_at": "2026-09-08T09:00:00Z",
            },
            {
                "node_id": "PR_kw_ignored",
                "number": 9,
                "title": "Pull request is not a task",
                "state": "open",
                "pull_request": {"url": "https://api.github.test/pulls/9"},
                "updated_at": "2026-09-08T08:00:00Z",
            },
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "github.test" and path == "/login/oauth/access_token":
            body = parse_qs(request.content.decode())
            assert body["code"] == ["oauth-code"]
            assert len(body["code_verifier"][0]) == 64
            return httpx.Response(
                200, json={"access_token": "ghu_ephemeral", "token_type": "bearer"}
            )
        if path == "/user/installations":
            assert request.headers["Authorization"] == "Bearer ghu_ephemeral"
            return httpx.Response(200, json={"installations": [{"id": 42}]})
        if path == "/app/installations/42" and request.method == "GET":
            assert request.headers["Authorization"].startswith("Bearer eyJ")
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "account": {"login": "acme", "type": "Organization"},
                    "permissions": {"metadata": "read", "issues": "read"},
                    "suspended_at": None,
                },
            )
        if path == "/applications/Iv1.dayfinchtest/token":
            assert request.method == "DELETE"
            assert request.headers["Authorization"].startswith("Basic ")
            self.oauth_revoked = True
            return httpx.Response(204)
        if path == "/app/installations/42/access_tokens":
            requested = json.loads(request.content)
            assert requested["permissions"] == {"issues": "read", "metadata": "read"}
            return httpx.Response(
                201,
                json={
                    "token": "ghs_installation_token_without_fixed_length",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
            )
        if path == "/installation/repositories":
            assert request.headers["Authorization"].startswith("Bearer ghs_")
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {"id": 100, "full_name": "acme/widget", "private": True}
                    ]
                },
            )
        if path == "/repos/acme/widget/issues":
            assert request.url.params["state"] == "all"
            assert request.url.params["per_page"] == "100"
            return httpx.Response(200, json=self.issues)
        raise AssertionError(
            f"Unexpected GitHub request: {request.method} {request.url}"
        )


def _signed_webhook(secret: str, payload: dict) -> tuple[bytes, str]:
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, signature


def test_github_app_connection_sync_webhooks_and_revocation(tmp_path, postgres_url):
    private_key_b64, _ = _github_key()
    settings = _settings(tmp_path, postgres_url, private_key_b64)
    contract = GitHubContract()
    app = create_app(settings, github_transport=httpx.MockTransport(contract))
    with TestClient(app) as client:
        database = app.state.database
        owner = database.get_user_by_email(settings.admin_email)
        member = _member(database, owner)
        project = database.create_project("GitHub project", "", owner["id"])
        _login(client, owner["email"], settings.admin_password)

        settings_page = client.get("/settings")
        assert settings_page.status_code == 200
        assert "Install GitHub App" in settings_page.text
        assert settings.github_client_secret not in settings_page.text
        assert settings.github_webhook_secret not in settings_page.text

        install = client.get("/integrations/github/install", follow_redirects=False)
        assert install.status_code == 303
        assert install.headers["location"].endswith(
            "/apps/dayfinch-test/installations/new"
        )
        setup = client.get(
            "/integrations/github/setup?installation_id=42", follow_redirects=False
        )
        assert setup.status_code == 303
        oauth_query = parse_qs(urlparse(setup.headers["location"]).query)
        assert oauth_query["code_challenge_method"] == ["S256"]
        assert len(oauth_query["code_challenge"][0]) == 43

        callback = client.get(
            "/integrations/github/callback",
            params={"code": "oauth-code", "state": oauth_query["state"][0]},
            follow_redirects=False,
        )
        assert callback.status_code == 303, callback.text
        assert contract.oauth_revoked
        integration_id = callback.headers["location"].rsplit("/", 1)[-1]
        integration = database.get_github_integration(integration_id)
        assert integration["provider_external_id"] == 42
        assert integration["secret_ciphertext"] == ""

        detail = client.get(callback.headers["location"])
        assert detail.status_code == 200
        assert "acme/widget" in detail.text
        mapped = client.post(
            f"/integrations/github/{integration_id}/mappings",
            data={
                "csrf": _csrf(detail),
                "repository": "100:acme/widget",
                "project_id": project["id"],
            },
            follow_redirects=False,
        )
        assert mapped.status_code == 303, mapped.text

        synced = client.post(
            f"/integrations/github/{integration_id}/sync",
            data={"csrf": _csrf(client.get(mapped.headers["location"]))},
            follow_redirects=False,
        )
        assert synced.status_code == 303, synced.text
        tasks = database.list_tasks(project["id"], include_archived=True)
        assert len(tasks) == 2
        active = next(task for task in tasks if task["external_key"] == "I_kw_open")
        closed = next(task for task in tasks if task["external_key"] == "I_kw_closed")
        assert active["status"] == "active"
        assert active["external_read_only"] is True
        assert active["external_url"].endswith("/issues/7")
        assert closed["status"] == "archived"
        assert database.get_github_integration(integration_id)["last_error_code"] == ""
        mapping = database.github_project_mappings(integration_id)[0]
        assert mapping["last_full_sync_at"]
        assert mapping["last_incremental_sync_at"]
        assert (
            sum(
                request.url.path == "/app/installations/42/access_tokens"
                for request in contract.requests
            )
            == 1
        )

        original_issues = contract.issues
        contract.issues = [original_issues[1]]
        incremental = client.post(
            f"/integrations/github/{integration_id}/sync",
            data={"csrf": _csrf(client.get(f"/integrations/github/{integration_id}"))},
            follow_redirects=False,
        )
        assert incremental.status_code == 303, incremental.text
        assert database.get_task(active["id"])["status"] == "active"
        issue_requests = [
            request
            for request in contract.requests
            if request.url.path == "/repos/acme/widget/issues"
        ]
        assert "since" not in issue_requests[0].url.params
        assert issue_requests[1].url.params["since"].endswith("Z")
        contract.issues = original_issues

        device, _ = database.create_device(
            "Owner development machine", owner["id"], project["id"]
        )
        session = database.sync_work_session(
            device, "active", active["id"], project["id"]
        )
        webhook_issue = {
            **contract.issues[0],
            "title": "Implemented synchronization",
            "state": "closed",
            "updated_at": "2026-09-08T11:00:00Z",
        }
        payload = {
            "action": "closed",
            "installation": {"id": 42},
            "repository": {"id": 100, "full_name": "acme/widget"},
            "issue": webhook_issue,
        }
        body, signature = _signed_webhook(settings.github_webhook_secret, payload)
        webhook = client.post(
            "/integrations/github/webhook",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "delivery-1",
                "X-Hub-Signature-256": signature,
            },
        )
        assert webhook.status_code == 202
        assert database.get_task(active["id"])["status"] == "archived"
        assert database.get_work_session(session["id"])["status"] == "stopped"

        duplicate_payload = {**payload, "issue": {**webhook_issue, "state": "open"}}
        duplicate_body, duplicate_signature = _signed_webhook(
            settings.github_webhook_secret, duplicate_payload
        )
        duplicate = client.post(
            "/integrations/github/webhook",
            content=duplicate_body,
            headers={
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "delivery-1",
                "X-Hub-Signature-256": duplicate_signature,
            },
        )
        assert duplicate.status_code == 202
        assert database.get_task(active["id"])["status"] == "archived"

        stale_payload = {
            **payload,
            "issue": {
                **webhook_issue,
                "state": "open",
                "updated_at": "2026-09-08T10:30:00Z",
            },
        }
        stale_body, stale_signature = _signed_webhook(
            settings.github_webhook_secret, stale_payload
        )
        assert (
            client.post(
                "/integrations/github/webhook",
                content=stale_body,
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-2",
                    "X-Hub-Signature-256": stale_signature,
                },
            ).status_code
            == 202
        )
        assert database.get_task(active["id"])["status"] == "archived"
        assert (
            client.post(
                "/integrations/github/webhook",
                content=body,
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-bad",
                    "X-Hub-Signature-256": "sha256=bad",
                },
            ).status_code
            == 401
        )
        oversized = b"x" * 1_000_001
        assert (
            client.post(
                "/integrations/github/webhook",
                content=oversized,
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-oversized",
                    "X-Hub-Signature-256": "sha256=unused",
                },
            ).status_code
            == 413
        )

        project_page = client.get(f"/projects/{project['id']}")
        assert "GitHub managed" in project_page.text
        manual_restore = client.post(
            f"/projects/{project['id']}/tasks/{active['id']}/status",
            data={"csrf": _csrf(project_page), "task_status": "active"},
        )
        assert manual_restore.status_code == 422
        assert "external integration" in manual_restore.text

        reopened_at = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
        assert (
            database.apply_github_issue(
                integration_id,
                100,
                "acme/widget",
                {
                    **contract.issues[0],
                    "state": "open",
                    "updated_at": reopened_at,
                },
                reopened_at,
            )
            == active["id"]
        )
        retained_session = database.sync_work_session(
            device, "active", active["id"], project["id"]
        )

        disconnected = client.post(
            f"/integrations/github/{integration_id}/disconnect",
            data={"csrf": _csrf(client.get(f"/integrations/github/{integration_id}"))},
            follow_redirects=False,
        )
        assert disconnected.status_code == 303
        assert database.get_github_integration(integration_id)["enabled"] is False
        retained = database.get_task(active["id"])
        assert retained["status"] == "active"
        assert retained["external_read_only"] is False
        assert database.get_work_session(retained_session["id"])["status"] == "active"
        assert "GitHub managed" not in client.get(f"/projects/{project['id']}").text

        assert (
            database.upsert_github_installation(42, "acme", "Organization", owner["id"])
            == integration_id
        )
        reattached_at = reopened_at + timedelta(minutes=1)
        assert (
            database.apply_github_issue(
                integration_id,
                100,
                "acme/widget",
                {
                    **contract.issues[0],
                    "state": "open",
                    "updated_at": reattached_at,
                },
                reattached_at,
            )
            == active["id"]
        )
        assert database.get_task(active["id"])["external_read_only"] is True
        assert database.get_work_session(retained_session["id"])["status"] == "active"

        _login(client, member["email"], "member password long enough")
        assert client.get(f"/integrations/github/{integration_id}").status_code == 403
        assert (
            client.post(
                f"/integrations/github/{integration_id}/sync",
                data={"csrf": "invalid"},
            ).status_code
            == 403
        )


def test_github_app_jwt_retries_rate_limits_and_installation_ownership(
    tmp_path, postgres_url
):
    private_key_b64, key = _github_key()
    settings = _settings(tmp_path, postgres_url, private_key_b64)
    attempts = 0

    def retrying_transport(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "id": 42,
                "account": {"login": "acme", "type": "Organization"},
                "permissions": {"issues": "read"},
                "suspended_at": None,
            },
        )

    service = GitHubAppService(
        settings,
        SimpleNamespace(),
        transport=httpx.MockTransport(retrying_transport),
        sleep=lambda _seconds: None,
    )
    observed = datetime(2026, 9, 8, 12, tzinfo=UTC)
    token = service.app_jwt(observed)
    header, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    assert claims == {
        "exp": int(observed.timestamp()) + 540,
        "iat": int(observed.timestamp()) - 60,
        "iss": settings.github_client_id,
    }
    key.public_key().verify(
        base64.urlsafe_b64decode(signature + "=="),
        f"{header}.{payload}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    assert service.installation(42)["account_login"] == "acme"
    assert attempts == 3
    service.close()

    def rate_limited(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"X-RateLimit-Remaining": "0", "Retry-After": "120"},
        )

    limited = GitHubAppService(
        settings,
        SimpleNamespace(),
        transport=httpx.MockTransport(rate_limited),
        sleep=lambda _seconds: None,
    )
    with pytest.raises(GitHubIntegrationError) as error:
        limited.installation(42)
    assert error.value.error_code == "rate_limited"
    assert error.value.retry_seconds == 120
    limited.close()


def test_github_configuration_is_all_or_nothing(tmp_path, postgres_url):
    settings = Settings(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024,
        retention_days=30,
        database_url=postgres_url,
        github_app_slug="configured-alone",
    )
    with pytest.raises(RuntimeError, match="must be configured together"):
        settings.prepare()


def test_github_sync_claims_scale_across_workers_and_failed_webhooks_retry(
    tmp_path, postgres_url
):
    settings = Settings(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024,
        retention_days=30,
        admin_email="owner@example.test",
        database_url=postgres_url,
    )
    app = create_app(settings)
    with TestClient(app):
        database = app.state.database
        owner = database.get_user_by_email(settings.admin_email)
        integration_ids = {
            database.upsert_github_installation(
                installation_id,
                f"account-{installation_id}",
                "Organization",
                owner["id"],
            )
            for installation_id in (42, 43)
        }
        observed = datetime.now(UTC) + timedelta(seconds=1)
        first = database.claim_due_github_integrations(
            observed, "11111111-1111-1111-1111-111111111111", limit=1
        )
        second = database.claim_due_github_integrations(
            observed, "22222222-2222-2222-2222-222222222222", limit=1
        )
        assert {first[0]["id"], second[0]["id"]} == integration_ids
        assert not database.claim_due_github_integrations(
            observed, "33333333-3333-3333-3333-333333333333"
        )

        database.mark_github_sync_failed(
            first[0]["id"],
            "11111111-1111-1111-1111-111111111111",
            observed,
            "rate_limited",
            retry_seconds=120,
        )
        failed = database.get_github_integration(first[0]["id"])
        assert failed["last_error_code"] == "rate_limited"
        assert failed["consecutive_failures"] == 1
        assert datetime.fromisoformat(failed["next_sync_at"]) == observed + timedelta(
            seconds=120
        )

        with database.connect() as connection:
            connection.execute(
                "UPDATE integrations SET sync_claim_until=%s WHERE id=%s",
                (observed - timedelta(seconds=1), second[0]["id"]),
            )
        reclaimed = database.claim_due_github_integrations(
            observed, "44444444-4444-4444-4444-444444444444"
        )
        assert [row["id"] for row in reclaimed] == [second[0]["id"]]

        project = database.create_project("Concurrent reconciliation", "", owner["id"])
        repository_id = 9001
        database.set_github_project_mapping(
            first[0]["id"], repository_id, "acme/concurrent", project["id"]
        )
        snapshot_at = database.github_sync_snapshot_at()
        task_id = database.apply_github_issue(
            first[0]["id"],
            repository_id,
            "acme/concurrent",
            {
                "node_id": "I_after_snapshot",
                "number": 1,
                "title": "Delivered during reconciliation",
                "body": "",
                "state": "open",
                "html_url": "https://github.test/acme/concurrent/issues/1",
                "updated_at": observed,
            },
            observed,
        )
        assert task_id
        assert (
            database.archive_missing_github_issues(
                first[0]["id"], repository_id, [], snapshot_at
            )
            == 0
        )
        assert database.get_task(task_id)["status"] == "active"

        assert database.begin_integration_webhook(
            "github", "retry-delivery", "issues", observed
        )
        database.finish_integration_webhook("github", "retry-delivery", "failed")
        assert database.begin_integration_webhook(
            "github", "retry-delivery", "issues", observed + timedelta(seconds=1)
        )
        database.finish_integration_webhook("github", "retry-delivery", "processed")
        assert not database.begin_integration_webhook(
            "github", "retry-delivery", "issues", observed + timedelta(seconds=2)
        )

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode, urlparse
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from ..config import Settings
from ..database import Database

LOGGER = logging.getLogger("dayfinch-github-integration")
MAX_GITHUB_REPOSITORIES = 10_000
MAX_GITHUB_ISSUES_PER_REPOSITORY = 10_000
MAX_GITHUB_WEBHOOK_BYTES = 1_000_000
GITHUB_FULL_SYNC_INTERVAL = timedelta(days=1)
GITHUB_INCREMENTAL_OVERLAP = timedelta(minutes=5)


class GitHubIntegrationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "github_unavailable",
        retry_seconds: int = 300,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.retry_seconds = min(max(retry_seconds, 30), 21_600)


class GitHubConfigurationError(GitHubIntegrationError):
    pass


@dataclass(frozen=True)
class _InstallationToken:
    value: str
    expires_at: datetime


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _as_utc_datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class GitHubAppService:
    """GitHub App installation authentication plus issue-to-task reconciliation."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ):
        self.settings = settings
        self.database = database
        self.enabled = settings.github_enabled
        self._sleep = sleep
        self._private_key: RSAPrivateKey | None = None
        if self.enabled:
            key = serialization.load_pem_private_key(
                base64.b64decode(settings.github_private_key_b64, validate=True),
                password=None,
            )
            if not isinstance(key, RSAPrivateKey):
                raise GitHubConfigurationError("The GitHub App private key must be RSA")
            self._private_key = key
        timeout = httpx.Timeout(10.0, connect=5.0)
        self._api = httpx.Client(
            base_url=settings.github_api_url,
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )
        self._web = httpx.Client(
            base_url=settings.github_web_url,
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )
        self._token_lock = threading.Lock()
        self._installation_tokens: dict[int, _InstallationToken] = {}

    def close(self) -> None:
        self._api.close()
        self._web.close()

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise GitHubConfigurationError(
                "GitHub integration is not configured",
                error_code="not_configured",
            )

    def app_jwt(self, now: datetime | None = None) -> str:
        self._require_enabled()
        assert self._private_key is not None
        observed = now or datetime.now(UTC)
        timestamp = int(observed.timestamp())
        header = _b64url(b'{"alg":"RS256","typ":"JWT"}')
        payload = _b64url(
            json.dumps(
                {
                    "iat": timestamp - 60,
                    "exp": timestamp + 9 * 60,
                    "iss": self.settings.github_client_id,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        )
        signing_input = f"{header}.{payload}".encode()
        signature = self._private_key.sign(
            signing_input, padding.PKCS1v15(), hashes.SHA256()
        )
        return f"{header}.{payload}.{_b64url(signature)}"

    def installation_url(self) -> str:
        self._require_enabled()
        return (
            f"{self.settings.github_web_url}/apps/"
            f"{quote(self.settings.github_app_slug, safe='-')}/installations/new"
        )

    def authorization_url(self, state: str, code_challenge: str) -> str:
        self._require_enabled()
        query = urlencode(
            {
                "client_id": self.settings.github_client_id,
                "redirect_uri": (
                    f"{self.settings.public_url}/integrations/github/callback"
                ),
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "allow_signup": "false",
            }
        )
        return f"{self.settings.github_web_url}/login/oauth/authorize?{query}"

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": self.settings.github_api_version,
            "User-Agent": "Dayfinch-GitHub-Integration",
        }

    def _request(
        self,
        client: httpx.Client,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        params: dict[str, object] | None = None,
        json_body: dict[str, object] | None = None,
        form_data: dict[str, object] | None = None,
    ) -> httpx.Response:
        response: httpx.Response | None = None
        for attempt in range(3):
            try:
                response = client.request(
                    method,
                    path,
                    headers=headers,
                    params=params,
                    json=json_body,
                    data=form_data,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == 2:
                    raise GitHubIntegrationError(
                        "GitHub could not be reached",
                        error_code="network_error",
                    ) from exc
            else:
                if response.status_code < 500:
                    break
                if attempt == 2:
                    raise GitHubIntegrationError(
                        "GitHub is temporarily unavailable",
                        error_code="provider_5xx",
                    )
            self._sleep(0.25 * (2**attempt))
        assert response is not None
        remaining = response.headers.get("X-RateLimit-Remaining")
        if response.status_code == 429 or (
            response.status_code == 403 and remaining == "0"
        ):
            retry_after = response.headers.get("Retry-After")
            reset = response.headers.get("X-RateLimit-Reset")
            retry_seconds = 300
            if retry_after and retry_after.isdigit():
                retry_seconds = int(retry_after)
            elif reset and reset.isdigit():
                retry_seconds = max(30, int(reset) - int(time.time()))
            raise GitHubIntegrationError(
                "GitHub rate limit reached; synchronization will retry later",
                error_code="rate_limited",
                retry_seconds=retry_seconds,
            )
        if response.status_code in {401, 403}:
            raise GitHubIntegrationError(
                "GitHub rejected the app credentials or permissions",
                error_code="authentication_failed",
                retry_seconds=1800,
            )
        if response.status_code == 404:
            raise GitHubIntegrationError(
                "The GitHub installation or repository is unavailable",
                error_code="not_found",
                retry_seconds=1800,
            )
        if response.is_error:
            raise GitHubIntegrationError(
                "GitHub rejected the integration request",
                error_code="provider_rejected",
            )
        return response

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubIntegrationError(
                "GitHub returned an invalid response",
                error_code="invalid_response",
            ) from exc
        if not isinstance(payload, dict):
            raise GitHubIntegrationError(
                "GitHub returned an invalid response",
                error_code="invalid_response",
            )
        return payload

    def exchange_user_code(self, code: str, code_verifier: str) -> str:
        self._require_enabled()
        if not code or len(code) > 500 or len(code_verifier) != 64:
            raise GitHubIntegrationError(
                "GitHub authorization response was invalid",
                error_code="invalid_oauth_response",
            )
        response = self._request(
            self._web,
            "POST",
            "/login/oauth/access_token",
            headers={
                "Accept": "application/json",
                "User-Agent": "Dayfinch-GitHub-Integration",
            },
            form_data={
                "client_id": self.settings.github_client_id,
                "client_secret": self.settings.github_client_secret,
                "code": code,
                "redirect_uri": (
                    f"{self.settings.public_url}/integrations/github/callback"
                ),
                "code_verifier": code_verifier,
            },
        )
        payload = self._json_object(response)
        token = str(payload.get("access_token") or "")
        if not token or len(token) > 2000 or payload.get("token_type") != "bearer":
            raise GitHubIntegrationError(
                "GitHub did not issue a valid user token",
                error_code="invalid_oauth_response",
            )
        return token

    def revoke_user_token(self, user_token: str) -> None:
        if not user_token:
            return
        basic = base64.b64encode(
            (
                f"{self.settings.github_client_id}:{self.settings.github_client_secret}"
            ).encode()
        ).decode()
        try:
            self._request(
                self._api,
                "DELETE",
                f"/applications/{quote(self.settings.github_client_id, safe='')}/token",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Basic {basic}",
                    "X-GitHub-Api-Version": self.settings.github_api_version,
                    "User-Agent": "Dayfinch-GitHub-Integration",
                },
                json_body={"access_token": user_token},
            )
        except GitHubIntegrationError:
            # The user token is used only for the installation ownership check and
            # is never persisted. A failed revocation is still surfaced in logs.
            LOGGER.warning("github_ephemeral_user_token_revocation_failed")

    def user_can_access_installation(
        self, user_token: str, installation_id: int
    ) -> bool:
        for page in range(1, 101):
            response = self._request(
                self._api,
                "GET",
                "/user/installations",
                headers=self._headers(user_token),
                params={"per_page": 100, "page": page},
            )
            payload = self._json_object(response)
            installations = payload.get("installations")
            if not isinstance(installations, list):
                raise GitHubIntegrationError(
                    "GitHub returned an invalid installation list",
                    error_code="invalid_response",
                )
            if any(
                isinstance(item, dict) and int(item.get("id") or 0) == installation_id
                for item in installations
            ):
                return True
            if len(installations) < 100:
                return False
        raise GitHubIntegrationError(
            "GitHub returned too many installations",
            error_code="response_too_large",
        )

    def installation(self, installation_id: int) -> dict[str, Any]:
        response = self._request(
            self._api,
            "GET",
            f"/app/installations/{installation_id}",
            headers=self._headers(self.app_jwt()),
        )
        payload = self._json_object(response)
        account = payload.get("account")
        if (
            int(payload.get("id") or 0) != installation_id
            or not isinstance(account, dict)
            or payload.get("suspended_at")
        ):
            raise GitHubIntegrationError(
                "The GitHub installation is unavailable",
                error_code="installation_unavailable",
            )
        permissions = payload.get("permissions")
        if not isinstance(permissions, dict) or permissions.get("issues") not in {
            "read",
            "write",
        }:
            raise GitHubIntegrationError(
                "Grant the GitHub App read-only Issues permission",
                error_code="missing_issues_permission",
            )
        return {
            "id": installation_id,
            "account_login": str(account.get("login") or ""),
            "account_type": str(account.get("type") or ""),
        }

    def installation_access_token(self, installation_id: int) -> str:
        self._require_enabled()
        now = datetime.now(UTC)
        with self._token_lock:
            cached = self._installation_tokens.get(installation_id)
            if cached and cached.expires_at > now + timedelta(minutes=5):
                return cached.value
            response = self._request(
                self._api,
                "POST",
                f"/app/installations/{installation_id}/access_tokens",
                headers=self._headers(self.app_jwt(now)),
                json_body={"permissions": {"issues": "read", "metadata": "read"}},
            )
            payload = self._json_object(response)
            token = str(payload.get("token") or "")
            try:
                expires_at = _as_utc_datetime(payload.get("expires_at"))
            except (TypeError, ValueError) as exc:
                raise GitHubIntegrationError(
                    "GitHub returned an invalid installation token",
                    error_code="invalid_response",
                ) from exc
            if not token or len(token) > 2000 or expires_at <= now:
                raise GitHubIntegrationError(
                    "GitHub returned an invalid installation token",
                    error_code="invalid_response",
                )
            self._installation_tokens[installation_id] = _InstallationToken(
                token, expires_at
            )
            return token

    def list_repositories(self, installation_id: int) -> list[dict[str, Any]]:
        token = self.installation_access_token(installation_id)
        repositories: list[dict[str, Any]] = []
        for page in range(1, 101):
            response = self._request(
                self._api,
                "GET",
                "/installation/repositories",
                headers=self._headers(token),
                params={"per_page": 100, "page": page},
            )
            payload = self._json_object(response)
            batch = payload.get("repositories")
            if not isinstance(batch, list):
                raise GitHubIntegrationError(
                    "GitHub returned an invalid repository list",
                    error_code="invalid_response",
                )
            for repository in batch:
                if not isinstance(repository, dict):
                    continue
                repository_id = int(repository.get("id") or 0)
                full_name = str(repository.get("full_name") or "")
                if repository_id > 0 and full_name.count("/") == 1:
                    repositories.append(
                        {
                            "id": repository_id,
                            "full_name": full_name[:255],
                            "private": bool(repository.get("private")),
                        }
                    )
            if len(repositories) > MAX_GITHUB_REPOSITORIES:
                raise GitHubIntegrationError(
                    "GitHub repository list exceeds the supported limit",
                    error_code="response_too_large",
                )
            if len(batch) < 100:
                break
        return repositories

    def list_issues(
        self,
        installation_id: int,
        repository_name: str,
        *,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        owner, separator, name = repository_name.partition("/")
        if not separator or not owner or not name:
            raise GitHubConfigurationError(
                "GitHub repository mapping is invalid",
                error_code="invalid_mapping",
            )
        token = self.installation_access_token(installation_id)
        issues: list[dict[str, Any]] = []
        path = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/issues"
        params: dict[str, str | int] = {"state": "all", "per_page": 100}
        if since is not None:
            if since.tzinfo is None:
                raise GitHubConfigurationError(
                    "GitHub synchronization cursor is invalid",
                    error_code="invalid_cursor",
                )
            params["since"] = since.astimezone(UTC).isoformat().replace("+00:00", "Z")
        for page in range(1, 102):
            params["page"] = page
            response = self._request(
                self._api,
                "GET",
                path,
                headers=self._headers(token),
                params=params,
            )
            try:
                batch = response.json()
            except ValueError as exc:
                raise GitHubIntegrationError(
                    "GitHub returned an invalid issue list",
                    error_code="invalid_response",
                ) from exc
            if not isinstance(batch, list):
                raise GitHubIntegrationError(
                    "GitHub returned an invalid issue list",
                    error_code="invalid_response",
                )
            for item in batch:
                if not isinstance(item, dict) or "pull_request" in item:
                    continue
                normalized = self.normalize_issue(item)
                if normalized:
                    issues.append(normalized)
            if len(issues) > MAX_GITHUB_ISSUES_PER_REPOSITORY:
                raise GitHubIntegrationError(
                    "GitHub issue list exceeds the supported limit",
                    error_code="response_too_large",
                )
            if len(batch) < 100:
                break
        return issues

    def normalize_issue(self, issue: dict[str, Any]) -> dict[str, Any] | None:
        if "pull_request" in issue:
            return None
        node_id = str(issue.get("node_id") or "")
        number = int(issue.get("number") or 0)
        state = str(issue.get("state") or "")
        title = str(issue.get("title") or "").replace("\x00", "")
        if (
            not node_id
            or len(node_id) > 500
            or number <= 0
            or state not in {"open", "closed"}
            or not title.strip()
        ):
            raise GitHubIntegrationError(
                "GitHub returned an invalid issue",
                error_code="invalid_response",
            )
        try:
            updated_at = _as_utc_datetime(issue.get("updated_at"))
        except (TypeError, ValueError) as exc:
            raise GitHubIntegrationError(
                "GitHub returned an invalid issue timestamp",
                error_code="invalid_response",
            ) from exc
        html_url = str(issue.get("html_url") or "")
        parsed_url = urlparse(html_url)
        expected_host = urlparse(self.settings.github_web_url).hostname
        if parsed_url.scheme != "https" or parsed_url.hostname != expected_host:
            html_url = ""
        return {
            "node_id": node_id,
            "number": number,
            "title": title[:1000],
            "body": str(issue.get("body") or "").replace("\x00", "")[:10_000],
            "state": state,
            "html_url": html_url,
            "updated_at": updated_at,
        }

    def sync_claimed(self, integration: dict[str, Any], claim_token: str) -> int:
        observed_at = datetime.now(UTC)
        snapshot_at = self.database.github_sync_snapshot_at()
        integration_id = integration["id"]
        installation_id = int(integration["provider_external_id"])
        try:
            accessible = {
                int(repository["id"]): repository
                for repository in self.list_repositories(installation_id)
            }
            synchronized = 0
            for mapping in self.database.github_project_mappings(integration_id):
                repository_id = int(mapping["external_repository_id"])
                repository = accessible.get(repository_id)
                if not repository:
                    self.database.remove_github_project_mapping(
                        integration_id, repository_id
                    )
                    continue
                if repository["full_name"] != mapping["external_repository_name"]:
                    self.database.set_github_project_mapping(
                        integration_id,
                        repository_id,
                        repository["full_name"],
                        mapping["project_id"],
                    )
                last_full_sync = (
                    _as_utc_datetime(mapping["last_full_sync_at"])
                    if mapping.get("last_full_sync_at")
                    else None
                )
                last_incremental_sync = (
                    _as_utc_datetime(mapping["last_incremental_sync_at"])
                    if mapping.get("last_incremental_sync_at")
                    else None
                )
                full_sync = (
                    last_full_sync is None
                    or observed_at - last_full_sync >= GITHUB_FULL_SYNC_INTERVAL
                )
                since = None
                if not full_sync:
                    cursor = last_incremental_sync or last_full_sync
                    assert cursor is not None
                    since = cursor - GITHUB_INCREMENTAL_OVERLAP
                issues = self.list_issues(
                    installation_id,
                    str(repository["full_name"]),
                    since=since,
                )
                seen_keys: list[str] = []
                for issue in issues:
                    task_id = self.database.apply_github_issue(
                        integration_id,
                        repository_id,
                        str(repository["full_name"]),
                        issue,
                        observed_at,
                    )
                    if task_id:
                        synchronized += 1
                        seen_keys.append(str(issue["node_id"]))
                if full_sync:
                    self.database.archive_missing_github_issues(
                        integration_id,
                        repository_id,
                        seen_keys,
                        snapshot_at,
                    )
                self.database.mark_github_mapping_synced(
                    integration_id,
                    repository_id,
                    observed_at,
                    full=full_sync,
                )
        except GitHubIntegrationError as exc:
            failures = int(integration.get("consecutive_failures") or 0)
            retry_seconds = max(
                exc.retry_seconds,
                min(21_600, 60 * (2 ** min(failures, 8))),
            )
            self.database.mark_github_sync_failed(
                integration_id,
                claim_token,
                observed_at,
                exc.error_code,
                retry_seconds=retry_seconds,
            )
            raise
        except (TypeError, ValueError) as exc:
            self.database.mark_github_sync_failed(
                integration_id,
                claim_token,
                observed_at,
                "invalid_configuration",
                retry_seconds=1800,
            )
            raise GitHubConfigurationError(
                "The GitHub integration mapping is invalid",
                error_code="invalid_configuration",
                retry_seconds=1800,
            ) from exc
        self.database.mark_github_sync_succeeded(
            integration_id, claim_token, observed_at
        )
        return synchronized

    def sync_now(self, integration_id: str) -> int:
        self._require_enabled()
        claim_token = str(uuid4())
        integration = self.database.claim_github_integration(
            integration_id, claim_token, datetime.now(UTC)
        )
        if not integration:
            raise GitHubIntegrationError(
                "This GitHub integration is already synchronizing or disconnected",
                error_code="sync_busy",
                retry_seconds=30,
            )
        return self.sync_claimed(integration, claim_token)

    def sync_due(self) -> int:
        if not self.enabled:
            return 0
        claim_token = str(uuid4())
        integrations = self.database.claim_due_github_integrations(
            datetime.now(UTC), claim_token
        )
        synchronized = 0
        for integration in integrations:
            try:
                synchronized += self.sync_claimed(integration, claim_token)
            except GitHubIntegrationError as exc:
                LOGGER.warning(
                    "github_sync_failed",
                    extra={"error_code": exc.error_code},
                )
        self.database.purge_integration_webhooks(
            datetime.now(UTC) - timedelta(days=90), limit=1000
        )
        return synchronized

    def verify_webhook(self, signature: str, body: bytes) -> bool:
        if not self.enabled or len(body) > MAX_GITHUB_WEBHOOK_BYTES:
            return False
        expected = (
            "sha256="
            + hmac.new(
                self.settings.github_webhook_secret.encode(), body, hashlib.sha256
            ).hexdigest()
        )
        return bool(signature) and hmac.compare_digest(expected, signature)

    def handle_webhook(
        self,
        event_name: str,
        delivery_id: str,
        payload: dict[str, Any],
    ) -> str:
        self._require_enabled()
        observed_at = datetime.now(UTC)
        if not self.database.begin_integration_webhook(
            "github", delivery_id, event_name, observed_at
        ):
            return "duplicate"
        outcome = "ignored"
        try:
            installation = payload.get("installation")
            installation_id = (
                int(installation.get("id") or 0)
                if isinstance(installation, dict)
                else 0
            )
            integration = self.database.get_github_integration_by_installation(
                installation_id
            )
            if not integration:
                return "ignored"
            if event_name == "issues":
                repository = payload.get("repository")
                issue = payload.get("issue")
                if not isinstance(repository, dict) or not isinstance(issue, dict):
                    raise GitHubIntegrationError(
                        "GitHub webhook payload was invalid",
                        error_code="invalid_webhook",
                    )
                normalized = self.normalize_issue(issue)
                if normalized:
                    if payload.get("action") == "deleted":
                        normalized["state"] = "closed"
                    task_id = self.database.apply_github_issue(
                        integration["id"],
                        int(repository.get("id") or 0),
                        str(repository.get("full_name") or ""),
                        normalized,
                        observed_at,
                    )
                    outcome = "processed" if task_id else "ignored"
            elif event_name == "installation_repositories":
                for repository in payload.get("repositories_removed") or []:
                    if not isinstance(repository, dict):
                        continue
                    try:
                        self.database.remove_github_project_mapping(
                            integration["id"], int(repository.get("id") or 0)
                        )
                    except ValueError:
                        continue
                    outcome = "processed"
            elif event_name == "installation" and payload.get("action") in {
                "deleted",
                "suspend",
            }:
                self.database.disconnect_github_integration(integration["id"])
                outcome = "processed"
            return outcome
        except Exception:
            outcome = "failed"
            raise
        finally:
            self.database.finish_integration_webhook("github", delivery_id, outcome)


def oauth_state_values() -> tuple[str, str, str]:
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)[:64]
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return state, verifier, challenge

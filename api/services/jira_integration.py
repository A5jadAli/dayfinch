from __future__ import annotations

import logging
import re
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time
from typing import Any
from urllib.parse import quote, urlencode, urlparse
from uuid import UUID, uuid4

import httpx

from ..config import Settings
from ..database import Database
from .integration_credentials import (
    ClaimedCredential,
    IntegrationCredentialError,
    IntegrationCredentialVault,
)

LOGGER = logging.getLogger("dayfinch-jira-integration")
JIRA_SCOPES = "offline_access read:jira-user read:jira-work write:jira-work"
MAX_JIRA_RESOURCES = 100
MAX_JIRA_PROJECTS = 10_000
MAX_JIRA_ISSUES_PER_PROJECT = 10_000
MAX_JIRA_RESPONSE_BYTES = 8 * 1024 * 1024
JIRA_FULL_SYNC_INTERVAL = timedelta(days=1)
JIRA_INCREMENTAL_OVERLAP = timedelta(minutes=5)
_RESOURCE_ID = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
_PROJECT_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,49}$")
_ISSUE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,49}-[1-9][0-9]{0,19}$")
_WORKLOG_ID = re.compile(r"^[1-9][0-9]{0,19}$")
JIRA_WORKLOG_PROPERTY = "dayfinch.export_id"
MAX_JIRA_WORKLOGS_PER_DAY = 10_000


class JiraIntegrationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "jira_unavailable",
        retry_seconds: int = 300,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.retry_seconds = min(max(retry_seconds, 30), 21_600)


class JiraConfigurationError(JiraIntegrationError):
    pass


def _as_utc(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise JiraIntegrationError(
            "Jira returned an invalid timestamp", error_code="invalid_response"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _adf_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.replace("\x00", "")[:10_000]
    fragments: list[str] = []
    nodes = 0
    characters = 0

    def visit(node: object, depth: int = 0) -> None:
        nonlocal characters, nodes
        if depth > 32 or nodes >= 10_000 or characters >= 10_000:
            return
        nodes += 1
        if isinstance(node, dict):
            if node.get("type") == "text" and isinstance(node.get("text"), str):
                text = node["text"].replace("\x00", "")[: 10_000 - characters]
                fragments.append(text)
                characters += len(text)
            elif node.get("type") in {"hardBreak", "paragraph", "heading", "listItem"}:
                fragments.append("\n")
                characters += 1
            content = node.get("content")
            if isinstance(content, list):
                for child in content:
                    visit(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                visit(child, depth + 1)

    visit(value)
    return "\n".join(
        line.strip() for line in "".join(fragments).splitlines() if line.strip()
    )[:10_000]


class JiraCloudService:
    """Jira Cloud OAuth 3LO and read-only issue-to-task synchronization."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        vault: IntegrationCredentialVault | None,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ):
        self.settings = settings
        self.database = database
        self.vault = vault
        self.enabled = settings.jira_enabled
        if self.enabled and vault is None:
            raise JiraConfigurationError(
                "Jira credential encryption is not configured",
                error_code="not_configured",
            )
        self._sleep = sleep
        self._client = httpx.Client(
            timeout=httpx.Timeout(15.0, connect=5.0),
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": "Dayfinch-Jira-Integration"},
        )

    def close(self) -> None:
        self._client.close()

    def _require_enabled(self) -> None:
        if not self.enabled or self.vault is None:
            raise JiraConfigurationError(
                "Jira integration is not configured",
                error_code="not_configured",
            )

    def authorization_url(self, state: str) -> str:
        self._require_enabled()
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,200}", state):
            raise JiraIntegrationError("Jira OAuth state is invalid")
        query = urlencode(
            {
                "audience": "api.atlassian.com",
                "client_id": self.settings.jira_client_id,
                "scope": JIRA_SCOPES,
                "redirect_uri": f"{self.settings.public_url}/integrations/jira/callback",
                "state": state,
                "response_type": "code",
                "prompt": "consent",
            }
        )
        separator = "&" if "?" in self.settings.jira_authorize_url else "?"
        return f"{self.settings.jira_authorize_url}{separator}{query}"

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        if len(response.content) > MAX_JIRA_RESPONSE_BYTES:
            raise JiraIntegrationError(
                "Jira returned an oversized response", error_code="invalid_response"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise JiraIntegrationError(
                "Jira returned an invalid response", error_code="invalid_response"
            ) from exc

    def _request(
        self,
        method: str,
        url: str,
        *,
        token: str = "",
        json_body: dict[str, Any] | None = None,
        allow_error_status: bool = False,
        allowed_error_statuses: frozenset[int] = frozenset(),
    ) -> httpx.Response:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        response: httpx.Response | None = None
        # Never replay a non-idempotent POST after a lost response. In particular,
        # Jira may have committed a worklog even though the TCP response vanished;
        # the durable export property is reconciled on the next outbox attempt.
        attempts = 3 if method.upper() in {"GET", "HEAD", "PUT", "DELETE"} else 1
        for attempt in range(attempts):
            try:
                with self._client.stream(
                    method, url, headers=headers, json=json_body
                ) as streamed:
                    declared = streamed.headers.get("Content-Length", "")
                    if declared.isdigit() and int(declared) > MAX_JIRA_RESPONSE_BYTES:
                        raise JiraIntegrationError(
                            "Jira returned an oversized response",
                            error_code="invalid_response",
                        )
                    body = bytearray()
                    for chunk in streamed.iter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_JIRA_RESPONSE_BYTES:
                            raise JiraIntegrationError(
                                "Jira returned an oversized response",
                                error_code="invalid_response",
                            )
                    response = httpx.Response(
                        streamed.status_code,
                        headers=streamed.headers,
                        content=bytes(body),
                        request=streamed.request,
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == attempts - 1:
                    raise JiraIntegrationError(
                        "Jira could not be reached", error_code="network_error"
                    ) from exc
            else:
                if response.status_code < 500:
                    break
                if attempt == attempts - 1:
                    raise JiraIntegrationError(
                        "Jira is temporarily unavailable", error_code="provider_5xx"
                    )
            self._sleep(0.25 * (2**attempt))
        assert response is not None
        if response.status_code == 429:
            retry_header = response.headers.get("Retry-After", "")
            retry_seconds = int(retry_header) if retry_header.isdigit() else 300
            raise JiraIntegrationError(
                "Jira rate limit reached; synchronization will retry later",
                error_code="rate_limited",
                retry_seconds=retry_seconds,
            )
        if response.status_code in allowed_error_statuses:
            return response
        if response.status_code == 401:
            raise JiraIntegrationError(
                "Jira rejected the authorization",
                error_code="authentication_failed",
                retry_seconds=1800,
            )
        if response.status_code == 403:
            raise JiraIntegrationError(
                "The Jira user does not have permission for this operation",
                error_code="permission_denied",
                retry_seconds=3600,
            )
        if response.status_code == 404:
            raise JiraIntegrationError(
                "The Jira site or project is unavailable",
                error_code="not_found",
                retry_seconds=1800,
            )
        if response.is_error and not allow_error_status:
            raise JiraIntegrationError(
                "Jira rejected the integration request",
                error_code="provider_rejected",
            )
        return response

    def _token_values(
        self, response: httpx.Response, *, require_refresh: bool
    ) -> tuple[dict[str, str], datetime]:
        if response.status_code == 400:
            payload = self._json(response)
            error_name = payload.get("error") if isinstance(payload, dict) else ""
            if error_name == "invalid_grant":
                raise JiraIntegrationError(
                    "Jira authorization expired; reconnect the site",
                    error_code="reauthorization_required",
                    retry_seconds=3600,
                )
        if response.is_error:
            raise JiraIntegrationError(
                "Jira rejected the OAuth token request",
                error_code="authentication_failed",
                retry_seconds=1800,
            )
        payload = self._json(response)
        if not isinstance(payload, dict):
            raise JiraIntegrationError(
                "Jira returned an invalid OAuth response",
                error_code="invalid_response",
            )
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        scope = payload.get("scope")
        token_type = str(payload.get("token_type", "Bearer"))
        try:
            expires_in = int(payload.get("expires_in"))
        except (TypeError, ValueError) as exc:
            raise JiraIntegrationError(
                "Jira returned an invalid OAuth response",
                error_code="invalid_response",
            ) from exc
        if (
            not isinstance(access_token, str)
            or not 20 <= len(access_token) <= 8192
            or (require_refresh and not isinstance(refresh_token, str))
            or (isinstance(refresh_token, str) and not 20 <= len(refresh_token) <= 8192)
            or not isinstance(scope, str)
            or token_type.lower() != "bearer"
            or not 60 <= expires_in <= 86_400
        ):
            raise JiraIntegrationError(
                "Jira returned an invalid OAuth response",
                error_code="invalid_response",
            )
        granted = set(scope.split())
        if not set(JIRA_SCOPES.split()).issubset(granted):
            raise JiraIntegrationError(
                "Jira did not grant the required scopes",
                error_code="insufficient_scope",
                retry_seconds=3600,
            )
        values = {"access_token": access_token, "scope": " ".join(sorted(granted))}
        if isinstance(refresh_token, str):
            values["refresh_token"] = refresh_token
        return values, datetime.now(UTC) + timedelta(seconds=expires_in)

    def exchange_code(self, code: str) -> tuple[dict[str, str], datetime]:
        self._require_enabled()
        if not code or len(code) > 4096 or any(ord(char) < 32 for char in code):
            raise JiraIntegrationError("Jira authorization code is invalid")
        response = self._request(
            "POST",
            self.settings.jira_token_url,
            json_body={
                "grant_type": "authorization_code",
                "client_id": self.settings.jira_client_id,
                "client_secret": self.settings.jira_client_secret,
                "code": code,
                "redirect_uri": f"{self.settings.public_url}/integrations/jira/callback",
            },
            allow_error_status=True,
        )
        return self._token_values(response, require_refresh=True)

    def accessible_resources(self, access_token: str) -> list[dict[str, str]]:
        self._require_enabled()
        response = self._request(
            "GET",
            f"{self.settings.jira_api_url}/oauth/token/accessible-resources",
            token=access_token,
        )
        payload = self._json(response)
        if not isinstance(payload, list) or len(payload) > MAX_JIRA_RESOURCES:
            raise JiraIntegrationError(
                "Jira returned an invalid site list", error_code="invalid_response"
            )
        resources: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in payload:
            if not isinstance(item, dict):
                raise JiraIntegrationError(
                    "Jira returned an invalid site", error_code="invalid_response"
                )
            resource_id_value = item.get("id")
            name_value = item.get("name")
            url_value = item.get("url")
            resource_id = (
                resource_id_value if isinstance(resource_id_value, str) else ""
            )
            name = (
                " ".join(name_value.replace("\x00", "").split())
                if isinstance(name_value, str)
                else ""
            )
            url = url_value.rstrip("/") if isinstance(url_value, str) else ""
            scopes = item.get("scopes", [])
            parsed = urlparse(url)
            if (
                not _RESOURCE_ID.fullmatch(resource_id)
                or resource_id in seen
                or not name
                or len(name) > 255
                or parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
                or not isinstance(scopes, list)
                or not {"read:jira-user", "read:jira-work", "write:jira-work"}.issubset(
                    set(scopes)
                )
            ):
                raise JiraIntegrationError(
                    "Jira returned an invalid site", error_code="invalid_response"
                )
            seen.add(resource_id)
            resources.append({"id": resource_id, "name": name, "url": url})
        return resources

    def current_user(self, cloud_id: str, access_token: str) -> dict[str, str]:
        self._require_enabled()
        if not _RESOURCE_ID.fullmatch(cloud_id):
            raise JiraIntegrationError(
                "Jira returned an invalid site", error_code="invalid_response"
            )
        response = self._request(
            "GET",
            f"{self.settings.jira_api_url}/ex/jira/{quote(cloud_id, safe='')}"
            "/rest/api/3/myself",
            token=access_token,
        )
        payload = self._json(response)
        if not isinstance(payload, dict):
            raise JiraIntegrationError(
                "Jira returned an invalid user identity", error_code="invalid_response"
            )
        account_id_value = payload.get("accountId")
        display_name_value = payload.get("displayName")
        account_id = (
            account_id_value if isinstance(account_id_value, str) else ""
        ).strip()
        display_name = (
            " ".join(display_name_value.replace("\x00", "").split())
            if isinstance(display_name_value, str)
            else ""
        )
        if (
            not account_id
            or len(account_id) > 255
            or any(
                char.isspace() or ord(char) < 32 or ord(char) == 127
                for char in account_id
            )
            or not display_name
            or len(display_name) > 255
            or payload.get("active") is not True
            or payload.get("accountType") != "atlassian"
        ):
            raise JiraIntegrationError(
                "Jira returned an invalid user identity", error_code="invalid_response"
            )
        return {"account_id": account_id, "display_name": display_name}

    def store_authorization(
        self,
        integration_id: str,
        values: dict[str, str],
        access_expires_at: datetime,
    ) -> None:
        self._require_enabled()
        assert self.vault is not None
        try:
            self.vault.store(
                integration_id,
                "jira",
                values,
                access_expires_at=access_expires_at,
            )
        except IntegrationCredentialError as exc:
            raise JiraIntegrationError(
                "Jira authorization could not be stored",
                error_code="credential_storage_failed",
            ) from exc

    def store_user_authorization(
        self,
        integration_id: str,
        user_id: str,
        values: dict[str, str],
        access_expires_at: datetime,
    ) -> None:
        self._require_enabled()
        assert self.vault is not None
        try:
            self.vault.store(
                integration_id,
                "jira",
                values,
                access_expires_at=access_expires_at,
                subject_user_id=user_id,
            )
        except IntegrationCredentialError as exc:
            raise JiraIntegrationError(
                "Jira member authorization could not be stored",
                error_code="credential_storage_failed",
            ) from exc

    def _refresh(self, claim: ClaimedCredential) -> str:
        assert self.vault is not None
        refresh_token = claim.credential.values.get("refresh_token")
        if not isinstance(refresh_token, str):
            self._release_refresh(claim)
            raise JiraIntegrationError(
                "Jira refresh authorization is unavailable",
                error_code="reauthorization_required",
                retry_seconds=3600,
            )
        try:
            response = self._request(
                "POST",
                self.settings.jira_token_url,
                json_body={
                    "grant_type": "refresh_token",
                    "client_id": self.settings.jira_client_id,
                    "client_secret": self.settings.jira_client_secret,
                    "refresh_token": refresh_token,
                },
                allow_error_status=True,
            )
            values, expires_at = self._token_values(response, require_refresh=True)
            self.vault.replace_after_refresh(
                claim.credential.integration_id,
                "jira",
                claim.claim_token,
                claim.credential.revision,
                values,
                access_expires_at=expires_at,
                subject_user_id=claim.credential.subject_user_id,
            )
            return values["access_token"]
        except JiraIntegrationError:
            self._release_refresh(claim)
            raise
        except IntegrationCredentialError as exc:
            self._release_refresh(claim)
            raise JiraIntegrationError(
                "Jira authorization could not be rotated safely",
                error_code="credential_storage_failed",
                retry_seconds=300,
            ) from exc

    def _release_refresh(self, claim: ClaimedCredential) -> None:
        assert self.vault is not None
        try:
            self.vault.release_refresh(claim)
        except IntegrationCredentialError:
            # The lease expires independently. Keep the provider-facing error safe
            # and leave enough signal for operators without logging credentials.
            LOGGER.exception("jira_refresh_claim_release_failed")

    def _access_token(
        self, integration_id: str, subject_user_id: str | None = None
    ) -> str:
        self._require_enabled()
        assert self.vault is not None
        try:
            opened = self.vault.open(
                integration_id,
                "jira",
                subject_user_id=subject_user_id,
            )
        except IntegrationCredentialError as exc:
            raise JiraIntegrationError(
                "Jira authorization is unavailable",
                error_code="credential_unavailable",
                retry_seconds=1800,
            ) from exc
        expires_at = _as_utc(opened.access_expires_at)
        token = opened.values.get("access_token")
        if isinstance(token, str) and expires_at > datetime.now(UTC) + timedelta(
            seconds=60
        ):
            return token
        try:
            claim = self.vault.claim_for_refresh(
                integration_id,
                "jira",
                datetime.now(UTC),
                lease_seconds=90,
                subject_user_id=subject_user_id,
            )
        except IntegrationCredentialError as exc:
            raise JiraIntegrationError(
                "Jira authorization is unavailable",
                error_code="credential_unavailable",
                retry_seconds=300,
            ) from exc
        if claim:
            return self._refresh(claim)
        for _attempt in range(10):
            self._sleep(0.1)
            try:
                current = self.vault.open(
                    integration_id,
                    "jira",
                    subject_user_id=subject_user_id,
                )
            except IntegrationCredentialError as exc:
                raise JiraIntegrationError(
                    "Jira authorization is unavailable",
                    error_code="credential_unavailable",
                    retry_seconds=300,
                ) from exc
            current_expiry = _as_utc(current.access_expires_at)
            current_token = current.values.get("access_token")
            if (
                current.revision > opened.revision
                and isinstance(current_token, str)
                and current_expiry > datetime.now(UTC) + timedelta(seconds=30)
            ):
                return current_token
        raise JiraIntegrationError(
            "Jira authorization refresh is already in progress",
            error_code="refresh_busy",
            retry_seconds=30,
        )

    def access_token(self, integration_id: str) -> str:
        return self._access_token(integration_id)

    def user_access_token(self, integration_id: str, user_id: str) -> str:
        connection = self.database.jira_user_connection(integration_id, user_id)
        if (
            not connection
            or not connection["enabled"]
            or not connection["user_enabled"]
            or not connection["integration_enabled"]
        ):
            raise JiraIntegrationError(
                "Jira member authorization is unavailable",
                error_code="reauthorization_required",
                retry_seconds=3600,
            )
        if connection["credential_kind"] == "site":
            return self._access_token(integration_id)
        return self._access_token(integration_id, user_id)

    def _api_request(
        self,
        integration: dict[str, Any],
        method: str,
        endpoint: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        integration_id = str(integration["id"])
        cloud_id = quote(str(integration["provider_resource_key"]), safe="")
        url = f"{self.settings.jira_api_url}/ex/jira/{cloud_id}{endpoint}"
        token = self.access_token(integration_id)
        try:
            return self._request(method, url, token=token, json_body=json_body)
        except JiraIntegrationError as exc:
            if exc.error_code != "authentication_failed":
                raise
        self.database.expire_integration_access(
            integration_id, "jira", datetime.now(UTC)
        )
        token = self.access_token(integration_id)
        return self._request(method, url, token=token, json_body=json_body)

    def _user_api_request(
        self,
        integration: dict[str, Any],
        user_id: str,
        method: str,
        endpoint: str,
        *,
        json_body: dict[str, Any] | None = None,
        allowed_error_statuses: frozenset[int] = frozenset(),
    ) -> httpx.Response:
        integration_id = str(integration["id"])
        cloud_id = quote(str(integration["provider_resource_key"]), safe="")
        url = f"{self.settings.jira_api_url}/ex/jira/{cloud_id}{endpoint}"
        token = self.user_access_token(integration_id, user_id)
        try:
            return self._request(
                method,
                url,
                token=token,
                json_body=json_body,
                allowed_error_statuses=allowed_error_statuses,
            )
        except JiraIntegrationError as exc:
            if exc.error_code != "authentication_failed":
                raise
        member = self.database.jira_user_connection(integration_id, user_id)
        if not member:
            raise JiraIntegrationError(
                "Jira member authorization is unavailable",
                error_code="reauthorization_required",
                retry_seconds=3600,
            )
        if member["credential_kind"] == "site":
            self.database.expire_integration_access(
                integration_id, "jira", datetime.now(UTC)
            )
        else:
            self.database.expire_user_integration_access(
                integration_id, user_id, "jira", datetime.now(UTC)
            )
        token = self.user_access_token(integration_id, user_id)
        return self._request(
            method,
            url,
            token=token,
            json_body=json_body,
            allowed_error_statuses=allowed_error_statuses,
        )

    @staticmethod
    def _worklog_date(value: object) -> date:
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        try:
            return date.fromisoformat(str(value))
        except ValueError as exc:
            raise JiraIntegrationError(
                "Jira worklog date is invalid", error_code="invalid_worklog"
            ) from exc

    @staticmethod
    def _worklog_body(export: dict[str, Any]) -> dict[str, Any]:
        try:
            seconds = int(export["desired_seconds"])
        except (TypeError, ValueError, KeyError) as exc:
            raise JiraIntegrationError(
                "Jira worklog duration is invalid", error_code="invalid_worklog"
            ) from exc
        if not 1 <= seconds <= 2_147_483_647:
            raise JiraIntegrationError(
                "Jira worklog duration is invalid", error_code="invalid_worklog"
            )
        started = _as_utc(export.get("desired_started_at"))
        export_id = str(export.get("id", ""))
        try:
            # Parsing through UUID rejects control characters and non-canonical
            # identifiers without ever placing the value in a provider URL.
            normalized_export_id = str(UUID(export_id))
        except (ValueError, AttributeError) as exc:
            raise JiraIntegrationError(
                "Jira worklog identity is invalid", error_code="invalid_worklog"
            ) from exc
        return {
            "comment": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Tracked with Dayfinch"}],
                    }
                ],
            },
            "properties": [
                {
                    "key": JIRA_WORKLOG_PROPERTY,
                    "value": {"export_id": normalized_export_id, "version": 1},
                }
            ],
            "started": started.strftime("%Y-%m-%dT%H:%M:%S.000%z"),
            "timeSpentSeconds": seconds,
        }

    def _find_export_worklog(
        self,
        integration: dict[str, Any],
        export: dict[str, Any],
        issue_key: str,
    ) -> str | None:
        work_date = self._worklog_date(export.get("work_date"))
        day_start = datetime.combine(work_date, datetime_time.min, tzinfo=UTC)
        day_end = day_start + timedelta(days=1)
        start_at = 0
        matches: list[str] = []
        export_id = str(export["id"])
        user_id = str(export["user_id"])
        while True:
            self._renew_worklog_claim(export)
            query = urlencode(
                {
                    "startAt": start_at,
                    "maxResults": 100,
                    "startedAfter": int(day_start.timestamp() * 1000) - 1,
                    "startedBefore": int(day_end.timestamp() * 1000),
                }
            )
            response = self._user_api_request(
                integration,
                user_id,
                "GET",
                f"/rest/api/3/issue/{quote(issue_key, safe='-')}/worklog?{query}",
            )
            payload = self._json(response)
            if not isinstance(payload, dict) or not isinstance(
                payload.get("worklogs"), list
            ):
                raise JiraIntegrationError(
                    "Jira returned an invalid worklog list",
                    error_code="invalid_response",
                )
            try:
                response_start = int(payload.get("startAt"))
                total = int(payload.get("total"))
            except (TypeError, ValueError) as exc:
                raise JiraIntegrationError(
                    "Jira returned invalid worklog pagination",
                    error_code="invalid_response",
                ) from exc
            worklogs = payload["worklogs"]
            if (
                response_start != start_at
                or total < 0
                or total > MAX_JIRA_WORKLOGS_PER_DAY
                or len(worklogs) > 100
            ):
                raise JiraIntegrationError(
                    "Jira returned invalid worklog pagination",
                    error_code="invalid_response",
                )
            for worklog in worklogs:
                if not isinstance(worklog, dict):
                    raise JiraIntegrationError(
                        "Jira returned an invalid worklog",
                        error_code="invalid_response",
                    )
                worklog_id = worklog.get("id")
                author = worklog.get("author")
                properties = worklog.get("properties", [])
                if (
                    not isinstance(worklog_id, str)
                    or not _WORKLOG_ID.fullmatch(worklog_id)
                    or not isinstance(author, dict)
                    or not isinstance(properties, list)
                ):
                    raise JiraIntegrationError(
                        "Jira returned an invalid worklog",
                        error_code="invalid_response",
                    )
                if author.get("accountId") != export["atlassian_account_id"]:
                    continue
                property_present = any(
                    isinstance(item, dict) and item.get("key") == JIRA_WORKLOG_PROPERTY
                    for item in properties
                )
                if not property_present:
                    continue
                self._renew_worklog_claim(export)
                property_response = self._user_api_request(
                    integration,
                    user_id,
                    "GET",
                    f"/rest/api/3/issue/{quote(issue_key, safe='-')}/worklog/"
                    f"{worklog_id}/properties/{JIRA_WORKLOG_PROPERTY}",
                    allowed_error_statuses=frozenset({404}),
                )
                if property_response.status_code == 404:
                    continue
                property_payload = self._json(property_response)
                value = (
                    property_payload.get("value")
                    if isinstance(property_payload, dict)
                    else None
                )
                if isinstance(value, dict) and value.get("export_id") == export_id:
                    matches.append(worklog_id)
            next_start = start_at + len(worklogs)
            if next_start >= total:
                break
            if not worklogs or next_start <= start_at:
                raise JiraIntegrationError(
                    "Jira returned invalid worklog pagination",
                    error_code="invalid_response",
                )
            start_at = next_start
        unique_matches = sorted(set(matches))
        if len(unique_matches) > 1:
            raise JiraIntegrationError(
                "Multiple Jira worklogs have the same Dayfinch identity",
                error_code="duplicate_worklogs",
                retry_seconds=3600,
            )
        return unique_matches[0] if unique_matches else None

    def _renew_worklog_claim(self, export: dict[str, Any]) -> None:
        claim_token = str(export.get("claim_token") or "")
        if not claim_token or not self.database.renew_jira_worklog_export_claim(
            str(export.get("id") or ""),
            claim_token,
            datetime.now(UTC),
            lease_seconds=600,
        ):
            raise JiraIntegrationError(
                "Jira worklog delivery ownership was lost",
                error_code="worklog_claim_lost",
                retry_seconds=30,
            )

    def _deliver_claimed_worklog(
        self, export: dict[str, Any]
    ) -> tuple[str | None, int, datetime | None]:
        self._renew_worklog_claim(export)
        integration = self.database.get_jira_integration(str(export["integration_id"]))
        if not integration or not integration["enabled"]:
            raise JiraIntegrationError(
                "Jira integration not found", error_code="not_found"
            )
        issue_key = str(export.get("issue_key") or "")
        if not _ISSUE_KEY.fullmatch(issue_key):
            raise JiraIntegrationError(
                "Jira worklog issue is invalid", error_code="invalid_worklog"
            )
        if export["atlassian_account_id"] != export["connected_account_id"]:
            raise JiraIntegrationError(
                "Reconnect the Jira account that owns these worklogs",
                error_code="identity_conflict",
                retry_seconds=3600,
            )
        user_id = str(export["user_id"])
        endpoint = f"/rest/api/3/issue/{quote(issue_key, safe='-')}/worklog"
        provider_id = export.get("provider_worklog_id")
        desired_seconds = int(export["desired_seconds"])
        desired_started = (
            _as_utc(export["desired_started_at"]) if desired_seconds else None
        )
        if desired_seconds == 0:
            if provider_id:
                self._renew_worklog_claim(export)
                response = self._user_api_request(
                    integration,
                    user_id,
                    "DELETE",
                    f"{endpoint}/{provider_id}?notifyUsers=false&adjustEstimate=leave",
                    allowed_error_statuses=frozenset({404}),
                )
                if response.status_code not in {204, 404}:
                    raise JiraIntegrationError(
                        "Jira rejected worklog deletion",
                        error_code="provider_rejected",
                    )
            return None, 0, None

        if provider_id:
            self._renew_worklog_claim(export)
            response = self._user_api_request(
                integration,
                user_id,
                "PUT",
                f"{endpoint}/{provider_id}?notifyUsers=false&adjustEstimate=leave",
                json_body=self._worklog_body(export),
                allowed_error_statuses=frozenset({404}),
            )
            if response.status_code != 404:
                if response.status_code != 200:
                    raise JiraIntegrationError(
                        "Jira rejected worklog update",
                        error_code="provider_rejected",
                    )
                return str(provider_id), desired_seconds, desired_started

        recovered_id = self._find_export_worklog(integration, export, issue_key)
        if recovered_id:
            self._renew_worklog_claim(export)
            response = self._user_api_request(
                integration,
                user_id,
                "PUT",
                f"{endpoint}/{recovered_id}?notifyUsers=false&adjustEstimate=leave",
                json_body=self._worklog_body(export),
                allowed_error_statuses=frozenset({404}),
            )
            if response.status_code == 200:
                return recovered_id, desired_seconds, desired_started
            # It was deleted between reconciliation and update. A create with the
            # stable property is safe; any lost response is recovered next run.

        self._renew_worklog_claim(export)
        response = self._user_api_request(
            integration,
            user_id,
            "POST",
            f"{endpoint}?notifyUsers=false&adjustEstimate=leave",
            json_body=self._worklog_body(export),
        )
        if response.status_code != 201:
            raise JiraIntegrationError(
                "Jira rejected worklog creation", error_code="provider_rejected"
            )
        payload = self._json(response)
        worklog_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(worklog_id, str) or not _WORKLOG_ID.fullmatch(worklog_id):
            raise JiraIntegrationError(
                "Jira returned an invalid worklog", error_code="invalid_response"
            )
        return worklog_id, desired_seconds, desired_started

    def sync_due_worklogs(self) -> int:
        if not self.enabled:
            return 0
        observed_at = datetime.now(UTC)
        self.database.stage_due_jira_worklogs(observed_at)
        delivered = 0
        for _batch in range(4):
            claim_token = str(uuid4())
            exports = self.database.claim_due_jira_worklog_exports(
                datetime.now(UTC), claim_token, limit=25
            )
            if not exports:
                break
            for export in exports:
                try:
                    provider_id, seconds, started_at = self._deliver_claimed_worklog(
                        export
                    )
                    saved = self.database.mark_jira_worklog_export_succeeded(
                        str(export["id"]),
                        claim_token,
                        provider_id,
                        seconds,
                        started_at,
                        datetime.now(UTC),
                    )
                    if saved:
                        delivered += 1
                    else:
                        LOGGER.warning("jira_worklog_claim_lost")
                except JiraIntegrationError as exc:
                    retry = min(
                        21_600,
                        exc.retry_seconds
                        * (2 ** min(int(export.get("attempt_count", 0)), 5)),
                    )
                    self.database.mark_jira_worklog_export_failed(
                        str(export["id"]),
                        claim_token,
                        datetime.now(UTC),
                        exc.error_code,
                        retry,
                    )
                    LOGGER.warning(
                        "jira_worklog_sync_failed",
                        extra={"error_code": exc.error_code},
                    )
        return delivered

    def list_projects(
        self, integration_id: str, claim_token: str | None = None
    ) -> list[dict[str, str]]:
        integration = self.database.get_jira_integration(integration_id)
        if not integration or not integration["enabled"]:
            raise JiraIntegrationError(
                "Jira integration not found", error_code="not_found"
            )
        projects: list[dict[str, str]] = []
        start_at = 0
        while True:
            if claim_token and not self.database.renew_jira_sync_claim(
                integration_id, claim_token, datetime.now(UTC)
            ):
                raise JiraIntegrationError(
                    "Jira synchronization ownership was lost",
                    error_code="sync_claim_lost",
                    retry_seconds=30,
                )
            response = self._api_request(
                integration,
                "GET",
                "/rest/api/3/project/search?"
                + urlencode({"startAt": start_at, "maxResults": 50, "orderBy": "name"}),
            )
            payload = self._json(response)
            if not isinstance(payload, dict) or not isinstance(
                payload.get("values"), list
            ):
                raise JiraIntegrationError(
                    "Jira returned an invalid project list",
                    error_code="invalid_response",
                )
            values = payload["values"]
            for item in values:
                if not isinstance(item, dict):
                    raise JiraIntegrationError(
                        "Jira returned an invalid project",
                        error_code="invalid_response",
                    )
                project_id_value = item.get("id")
                project_key_value = item.get("key")
                project_name_value = item.get("name")
                project_id = (
                    project_id_value if isinstance(project_id_value, str) else ""
                )
                project_key = (
                    project_key_value if isinstance(project_key_value, str) else ""
                )
                project_name = (
                    " ".join(project_name_value.replace("\x00", "").split())
                    if isinstance(project_name_value, str)
                    else ""
                )
                if (
                    not _RESOURCE_ID.fullmatch(project_id)
                    or not _PROJECT_KEY.fullmatch(project_key)
                    or not project_name
                    or len(project_name) > 255
                ):
                    raise JiraIntegrationError(
                        "Jira returned an invalid project",
                        error_code="invalid_response",
                    )
                projects.append(
                    {"id": project_id, "key": project_key, "name": project_name}
                )
                if len(projects) > MAX_JIRA_PROJECTS:
                    raise JiraIntegrationError(
                        "Jira project limit exceeded", error_code="capacity_exceeded"
                    )
            if payload.get("isLast") is True or not values:
                break
            next_start = start_at + len(values)
            try:
                total = int(payload.get("total", next_start))
            except (TypeError, ValueError) as exc:
                raise JiraIntegrationError(
                    "Jira returned invalid pagination", error_code="invalid_response"
                ) from exc
            if next_start <= start_at or next_start >= total:
                break
            start_at = next_start
        return projects

    def _issue(self, integration: dict[str, Any], value: object) -> dict[str, Any]:
        if not isinstance(value, dict) or not isinstance(value.get("fields"), dict):
            raise JiraIntegrationError(
                "Jira returned an invalid issue", error_code="invalid_response"
            )
        fields = value["fields"]
        issue_id_value = value.get("id")
        issue_key_value = value.get("key")
        summary_value = fields.get("summary")
        issue_id = issue_id_value if isinstance(issue_id_value, str) else ""
        issue_key = issue_key_value if isinstance(issue_key_value, str) else ""
        summary = (
            " ".join(summary_value.replace("\x00", "").split())
            if isinstance(summary_value, str)
            else ""
        )
        status = fields.get("status")
        category = status.get("statusCategory") if isinstance(status, dict) else None
        category_key = (
            str(category.get("key", "")) if isinstance(category, dict) else ""
        )
        if (
            not _RESOURCE_ID.fullmatch(issue_id)
            or not _ISSUE_KEY.fullmatch(issue_key)
            or not summary
            or len(summary) > 500
            or category_key not in {"new", "indeterminate", "done"}
        ):
            raise JiraIntegrationError(
                "Jira returned an invalid issue", error_code="invalid_response"
            )
        site_url = str(integration["provider_resource_url"]).rstrip("/")
        return {
            "id": issue_id,
            "key": issue_key,
            "summary": summary,
            "description": _adf_text(fields.get("description")),
            "done": category_key == "done",
            "updated_at": _as_utc(fields.get("updated")),
            "url": f"{site_url}/browse/{quote(issue_key, safe='-')}",
        }

    def list_issues(
        self,
        integration: dict[str, Any],
        mapping: dict[str, Any],
        *,
        updated_since: datetime | None,
        claim_token: str,
    ) -> list[dict[str, Any]]:
        project_id = str(mapping["external_project_id"])
        if not _RESOURCE_ID.fullmatch(project_id):
            raise JiraIntegrationError(
                "Jira project mapping is invalid", error_code="invalid_mapping"
            )
        jql = f'project = "{project_id}"'
        if updated_since:
            jql += f' AND updated >= "{updated_since.astimezone(UTC):%Y-%m-%d %H:%M}"'
        jql += " ORDER BY updated ASC, id ASC"
        issues: list[dict[str, Any]] = []
        next_token = ""
        seen_tokens: set[str] = set()
        while True:
            if not self.database.renew_jira_sync_claim(
                str(integration["id"]), claim_token, datetime.now(UTC)
            ):
                raise JiraIntegrationError(
                    "Jira synchronization ownership was lost",
                    error_code="sync_claim_lost",
                    retry_seconds=30,
                )
            body: dict[str, Any] = {
                "jql": jql,
                "maxResults": 100,
                "fields": ["summary", "description", "status", "updated"],
            }
            if next_token:
                body["nextPageToken"] = next_token
            response = self._api_request(
                integration,
                "POST",
                "/rest/api/3/search/jql",
                json_body=body,
            )
            payload = self._json(response)
            if not isinstance(payload, dict) or not isinstance(
                payload.get("issues"), list
            ):
                raise JiraIntegrationError(
                    "Jira returned an invalid issue list", error_code="invalid_response"
                )
            for item in payload["issues"]:
                issues.append(self._issue(integration, item))
                if len(issues) > MAX_JIRA_ISSUES_PER_PROJECT:
                    raise JiraIntegrationError(
                        "Jira issue limit exceeded", error_code="capacity_exceeded"
                    )
            candidate = payload.get("nextPageToken")
            if not candidate:
                break
            if (
                not isinstance(candidate, str)
                or len(candidate) > 4096
                or candidate in seen_tokens
            ):
                raise JiraIntegrationError(
                    "Jira returned invalid pagination", error_code="invalid_response"
                )
            seen_tokens.add(candidate)
            next_token = candidate
        return issues

    @staticmethod
    def _timestamp(value: object) -> datetime | None:
        return _as_utc(value) if value else None

    def _sync_claimed(
        self,
        integration: dict[str, Any],
        claim_token: str,
        observed_at: datetime,
    ) -> int:
        integration_id = str(integration["id"])
        snapshot_at = self.database.integration_sync_snapshot_at()
        synchronized = 0
        try:
            accessible = {
                project["id"]: project
                for project in self.list_projects(integration_id, claim_token)
            }
            for mapping in self.database.jira_project_mappings(integration_id):
                external_id = str(mapping["external_project_id"])
                project = accessible.get(external_id)
                if not project:
                    self.database.remove_jira_project_mapping(
                        integration_id, external_id
                    )
                    continue
                self.database.set_jira_project_mapping(
                    integration_id,
                    external_id,
                    project["key"],
                    project["name"],
                    str(mapping["project_id"]),
                )
                full_at = self._timestamp(mapping.get("last_full_sync_at"))
                incremental_at = self._timestamp(
                    mapping.get("last_incremental_sync_at")
                )
                full = (
                    full_at is None or observed_at - full_at >= JIRA_FULL_SYNC_INTERVAL
                )
                updated_since = None
                if not full and incremental_at:
                    updated_since = incremental_at - JIRA_INCREMENTAL_OVERLAP
                issues = self.list_issues(
                    integration,
                    mapping,
                    updated_since=updated_since,
                    claim_token=claim_token,
                )
                seen_keys: list[str] = []
                for issue in issues:
                    seen_keys.append(f"jira:{issue['id']}")
                    if self.database.apply_jira_issue(
                        integration_id, external_id, issue, observed_at
                    ):
                        synchronized += 1
                if full:
                    self.database.archive_missing_jira_issues(
                        integration_id, external_id, seen_keys, snapshot_at
                    )
                self.database.mark_jira_mapping_synced(
                    integration_id, external_id, observed_at, full=full
                )
        except JiraIntegrationError as exc:
            self.database.mark_jira_sync_failed(
                integration_id,
                claim_token,
                observed_at,
                exc.error_code,
                retry_seconds=exc.retry_seconds,
            )
            raise
        except Exception as exc:
            self.database.mark_jira_sync_failed(
                integration_id,
                claim_token,
                observed_at,
                "internal_error",
                retry_seconds=300,
            )
            raise JiraIntegrationError(
                "Jira synchronization failed", error_code="internal_error"
            ) from exc
        self.database.mark_jira_sync_succeeded(integration_id, claim_token, observed_at)
        return synchronized

    def sync_now(self, integration_id: str) -> int:
        self._require_enabled()
        observed_at = datetime.now(UTC)
        claim_token = str(uuid4())
        integration = self.database.claim_jira_integration(
            integration_id, claim_token, observed_at
        )
        if not integration:
            raise JiraIntegrationError(
                "Jira synchronization is already running or unavailable",
                error_code="sync_busy",
                retry_seconds=30,
            )
        return self._sync_claimed(integration, claim_token, observed_at)

    def sync_due(self) -> int:
        if not self.enabled:
            return 0
        synchronized = 0
        for _attempt in range(10):
            observed_at = datetime.now(UTC)
            claim_token = str(uuid4())
            integrations = self.database.claim_due_jira_integrations(
                observed_at, claim_token, limit=1
            )
            if not integrations:
                break
            try:
                synchronized += self._sync_claimed(
                    integrations[0], claim_token, observed_at
                )
            except JiraIntegrationError as exc:
                LOGGER.warning("jira_sync_failed", extra={"error_code": exc.error_code})
        return synchronized

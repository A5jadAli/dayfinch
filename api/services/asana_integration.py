from __future__ import annotations

import logging
import re
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode
from uuid import UUID, uuid4

import httpx

from ..config import Settings
from ..database import Database
from .integration_credentials import (
    ClaimedCredential,
    IntegrationCredentialError,
    IntegrationCredentialVault,
    OpenedCredential,
)

LOGGER = logging.getLogger("dayfinch-asana-integration")
ASANA_SCOPES = (
    "openid profile email workspaces:read projects:read tasks:read "
    "stories:read stories:write"
)
MAX_ASANA_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_ASANA_WORKSPACES = 1_000
MAX_ASANA_PROJECTS = 10_000
MAX_ASANA_TASKS_PER_PROJECT = 10_000
MAX_ASANA_MAPPINGS = 10_000
ASANA_MAPPING_BATCH_SIZE = 10
MAX_ASANA_STORIES_PER_TASK = 10_000
_GID = re.compile(r"^[^\s/\x00-\x1f\x7f]{1,200}$")
_OFFSET = re.compile(r"^[A-Za-z0-9._~+/=-]{1,4096}$")


class AsanaIntegrationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "asana_unavailable",
        retry_seconds: int = 300,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.retry_seconds = min(max(int(retry_seconds), 30), 21_600)


class AsanaConfigurationError(AsanaIntegrationError):
    pass


def _as_utc(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AsanaIntegrationError(
            "Asana returned an invalid timestamp", error_code="invalid_response"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class AsanaCloudService:
    """Asana OAuth, bounded project/task synchronization, and token rotation."""

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
        self.enabled = settings.asana_enabled
        if self.enabled and vault is None:
            raise AsanaConfigurationError(
                "Asana credential encryption is not configured",
                error_code="not_configured",
            )
        self._sleep = sleep
        self._client = httpx.Client(
            timeout=httpx.Timeout(15.0, connect=5.0),
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": "Dayfinch-Asana-Integration"},
        )

    def close(self) -> None:
        self._client.close()

    def _require_enabled(self) -> None:
        if not self.enabled or self.vault is None:
            raise AsanaConfigurationError(
                "Asana integration is not configured", error_code="not_configured"
            )

    def authorization_url(self, state: str, challenge: str) -> str:
        self._require_enabled()
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,200}", state) or not re.fullmatch(
            r"[A-Za-z0-9_-]{43,128}", challenge
        ):
            raise AsanaIntegrationError("Asana OAuth state is invalid")
        query = urlencode(
            {
                "client_id": self.settings.asana_client_id,
                "redirect_uri": f"{self.settings.public_url}/integrations/asana/callback",
                "response_type": "code",
                "state": state,
                "scope": ASANA_SCOPES,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
            }
        )
        separator = "&" if "?" in self.settings.asana_authorize_url else "?"
        return f"{self.settings.asana_authorize_url}{separator}{query}"

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        if len(response.content) > MAX_ASANA_RESPONSE_BYTES:
            raise AsanaIntegrationError(
                "Asana returned an oversized response", error_code="invalid_response"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise AsanaIntegrationError(
                "Asana returned an invalid response", error_code="invalid_response"
            ) from exc

    def _request(
        self,
        method: str,
        url: str,
        *,
        token: str = "",
        form_body: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        allowed_error_statuses: frozenset[int] = frozenset(),
    ) -> httpx.Response:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        attempts = 3 if method.upper() in {"GET", "HEAD", "PUT", "DELETE"} else 1
        response: httpx.Response | None = None
        for attempt in range(attempts):
            try:
                with self._client.stream(
                    method,
                    url,
                    headers=headers,
                    data=form_body,
                    json=json_body,
                ) as streamed:
                    declared = streamed.headers.get("Content-Length", "")
                    if declared.isdigit() and int(declared) > MAX_ASANA_RESPONSE_BYTES:
                        raise AsanaIntegrationError(
                            "Asana returned an oversized response",
                            error_code="invalid_response",
                        )
                    body = bytearray()
                    for chunk in streamed.iter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_ASANA_RESPONSE_BYTES:
                            raise AsanaIntegrationError(
                                "Asana returned an oversized response",
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
                    raise AsanaIntegrationError(
                        "Asana could not be reached", error_code="network_error"
                    ) from exc
            else:
                if response.status_code < 500:
                    break
                if attempt == attempts - 1:
                    raise AsanaIntegrationError(
                        "Asana is temporarily unavailable", error_code="provider_5xx"
                    )
            self._sleep(0.25 * (2**attempt))
        assert response is not None
        if response.status_code == 429:
            retry_header = response.headers.get("Retry-After", "")
            retry_seconds = int(retry_header) if retry_header.isdigit() else 300
            raise AsanaIntegrationError(
                "Asana rate limit reached; synchronization will retry later",
                error_code="rate_limited",
                retry_seconds=retry_seconds,
            )
        if response.status_code in allowed_error_statuses:
            return response
        if response.status_code == 401:
            raise AsanaIntegrationError(
                "Asana rejected the authorization",
                error_code="authentication_failed",
                retry_seconds=1800,
            )
        if response.status_code == 403:
            raise AsanaIntegrationError(
                "The Asana user does not have permission for this operation",
                error_code="permission_denied",
                retry_seconds=3600,
            )
        if response.status_code == 404:
            raise AsanaIntegrationError(
                "The Asana workspace, project, or task is unavailable",
                error_code="not_found",
                retry_seconds=1800,
            )
        if response.is_error:
            raise AsanaIntegrationError(
                "Asana rejected the integration request",
                error_code="provider_rejected",
            )
        return response

    def _validate_scopes(self, access_token: str) -> set[str]:
        response = self._request(
            "POST",
            self.settings.asana_token_info_url,
            form_body={"token": access_token},
            allowed_error_statuses=frozenset({400}),
        )
        if response.status_code == 400:
            raise AsanaIntegrationError(
                "Asana authorization is invalid",
                error_code="authentication_failed",
                retry_seconds=1800,
            )
        payload = self._json(response)
        scope = payload.get("scope") if isinstance(payload, dict) else None
        if (
            not isinstance(scope, str)
            or payload.get("active") is not True
            or str(payload.get("token_type", "")).lower() != "bearer"
        ):
            raise AsanaIntegrationError(
                "Asana returned invalid token information",
                error_code="invalid_response",
            )
        granted = set(scope.split())
        if not set(ASANA_SCOPES.split()).issubset(granted):
            raise AsanaIntegrationError(
                "Asana did not grant the required scopes",
                error_code="insufficient_scope",
                retry_seconds=3600,
            )
        return granted

    def _token_values(
        self,
        response: httpx.Response,
        *,
        require_refresh: bool,
        prior: dict[str, Any] | None = None,
    ) -> tuple[dict[str, str], datetime]:
        if response.status_code == 400:
            payload = self._json(response)
            error_name = payload.get("error") if isinstance(payload, dict) else ""
            if error_name == "invalid_grant":
                raise AsanaIntegrationError(
                    "Asana authorization expired; reconnect the account",
                    error_code="reauthorization_required",
                    retry_seconds=3600,
                )
        if response.is_error:
            raise AsanaIntegrationError(
                "Asana rejected the OAuth token request",
                error_code="authentication_failed",
                retry_seconds=1800,
            )
        payload = self._json(response)
        if not isinstance(payload, dict):
            raise AsanaIntegrationError(
                "Asana returned an invalid OAuth response",
                error_code="invalid_response",
            )
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        token_type = str(payload.get("token_type", "bearer"))
        try:
            expires_in = int(payload.get("expires_in"))
        except (TypeError, ValueError) as exc:
            raise AsanaIntegrationError(
                "Asana returned an invalid OAuth response",
                error_code="invalid_response",
            ) from exc
        if refresh_token is None and prior:
            refresh_token = prior.get("refresh_token")
        if (
            not isinstance(access_token, str)
            or not 1 <= len(access_token) <= 8192
            or not isinstance(refresh_token, str)
            or not 1 <= len(refresh_token) <= 8192
            or (require_refresh and not refresh_token)
            or token_type.lower() != "bearer"
            or not 60 <= expires_in <= 86_400
        ):
            raise AsanaIntegrationError(
                "Asana returned an invalid OAuth response",
                error_code="invalid_response",
            )
        granted = self._validate_scopes(access_token)
        values: dict[str, str] = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "scope": " ".join(sorted(granted)),
        }
        identity = payload.get("data")
        if isinstance(identity, dict):
            user_gid = str(identity.get("gid") or identity.get("id") or "")
            display_name_value = identity.get("name")
            display_name = (
                " ".join(display_name_value.replace("\x00", "").split())
                if isinstance(display_name_value, str)
                else ""
            )
            if (
                not _GID.fullmatch(user_gid)
                or not display_name
                or len(display_name) > 255
            ):
                raise AsanaIntegrationError(
                    "Asana returned an invalid user identity",
                    error_code="invalid_response",
                )
            values["user_gid"] = user_gid
            values["display_name"] = display_name
        elif prior:
            for key in ("user_gid", "display_name"):
                value = prior.get(key)
                if isinstance(value, str):
                    values[key] = value
        if not values.get("user_gid") or not values.get("display_name"):
            raise AsanaIntegrationError(
                "Asana returned an invalid user identity",
                error_code="invalid_response",
            )
        return values, datetime.now(UTC) + timedelta(seconds=expires_in)

    def exchange_code(
        self, code: str, verifier: str
    ) -> tuple[dict[str, str], datetime]:
        self._require_enabled()
        if (
            not code
            or len(code) > 4096
            or any(ord(char) < 32 for char in code)
            or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier)
        ):
            raise AsanaIntegrationError("Asana authorization response is invalid")
        response = self._request(
            "POST",
            self.settings.asana_token_url,
            form_body={
                "grant_type": "authorization_code",
                "client_id": self.settings.asana_client_id,
                "client_secret": self.settings.asana_client_secret,
                "redirect_uri": f"{self.settings.public_url}/integrations/asana/callback",
                "code": code,
                "code_verifier": verifier,
            },
            allowed_error_statuses=frozenset({400}),
        )
        return self._token_values(response, require_refresh=True)

    @staticmethod
    def _identity(values: dict[str, Any]) -> dict[str, str]:
        gid = str(values.get("user_gid") or "")
        name = str(values.get("display_name") or "")
        if not _GID.fullmatch(gid) or not name or len(name) > 255:
            raise AsanaIntegrationError(
                "Asana user identity is unavailable", error_code="invalid_response"
            )
        return {"user_gid": gid, "display_name": name}

    def _page_items(
        self,
        url: str,
        token: str = "",
        *,
        maximum: int,
        renew: Callable[[], None] | None = None,
        request_page: Callable[[str], httpx.Response] | None = None,
    ) -> list[dict[str, Any]]:
        if bool(token) == bool(request_page):
            raise ValueError("Provide exactly one Asana page authorization method")
        items: list[dict[str, Any]] = []
        seen_offsets: set[str] = set()
        offset = ""
        while True:
            if renew:
                renew()
            separator = "&" if "?" in url else "?"
            page_url = url
            if offset:
                page_url = f"{url}{separator}{urlencode({'offset': offset})}"
            response = (
                request_page(page_url)
                if request_page is not None
                else self._request("GET", page_url, token=token)
            )
            payload = self._json(response)
            page = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(page, list) or any(
                not isinstance(item, dict) for item in page
            ):
                raise AsanaIntegrationError(
                    "Asana returned an invalid result page",
                    error_code="invalid_response",
                )
            items.extend(page)
            if len(items) > maximum:
                raise AsanaIntegrationError(
                    "Asana result exceeds the configured safety limit",
                    error_code="result_too_large",
                    retry_seconds=3600,
                )
            next_page = payload.get("next_page")
            if next_page is None:
                break
            next_offset = (
                next_page.get("offset") if isinstance(next_page, dict) else None
            )
            if (
                not isinstance(next_offset, str)
                or not _OFFSET.fullmatch(next_offset)
                or next_offset in seen_offsets
            ):
                raise AsanaIntegrationError(
                    "Asana returned invalid pagination",
                    error_code="invalid_response",
                )
            seen_offsets.add(next_offset)
            offset = next_offset
        return items

    def workspaces_with_token(self, access_token: str) -> list[dict[str, str]]:
        self._require_enabled()
        raw = self._page_items(
            f"{self.settings.asana_api_url}/workspaces?limit=100",
            access_token,
            maximum=MAX_ASANA_WORKSPACES,
        )
        workspaces: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in raw:
            gid = str(item.get("gid") or "")
            value = item.get("name")
            name = (
                " ".join(value.replace("\x00", "").split())
                if isinstance(value, str)
                else ""
            )
            if not _GID.fullmatch(gid) or gid in seen or not name or len(name) > 255:
                raise AsanaIntegrationError(
                    "Asana returned an invalid workspace",
                    error_code="invalid_response",
                )
            seen.add(gid)
            workspaces.append({"id": gid, "name": name})
        if not workspaces:
            raise AsanaIntegrationError(
                "The Asana account has no accessible workspace",
                error_code="no_workspace",
                retry_seconds=3600,
            )
        return workspaces

    def store_pending_site_authorization(
        self,
        actor_id: str,
        values: dict[str, str],
        access_expires_at: datetime,
        workspaces: list[dict[str, str]],
    ) -> str:
        self._require_enabled()
        assert self.vault is not None
        pending_id = str(uuid4())
        payload: dict[str, Any] = dict(values)
        payload["access_expires_at"] = access_expires_at.isoformat()
        payload["workspaces"] = workspaces
        try:
            self.vault.store_pending(
                pending_id,
                actor_id,
                "asana",
                payload,
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        except IntegrationCredentialError as exc:
            raise AsanaIntegrationError(
                "Asana workspace selection could not be stored",
                error_code="credential_storage_failed",
            ) from exc
        return pending_id

    def pending_site_authorization(
        self, pending_id: str, actor_id: str
    ) -> OpenedCredential:
        self._require_enabled()
        assert self.vault is not None
        try:
            pending = self.vault.open_pending(pending_id, actor_id, "asana")
        except IntegrationCredentialError as exc:
            raise AsanaIntegrationError(
                "Asana workspace selection expired; connect again",
                error_code="state_expired",
                retry_seconds=30,
            ) from exc
        workspaces = pending.values.get("workspaces")
        if not isinstance(workspaces, list) or not workspaces:
            raise AsanaIntegrationError(
                "Asana workspace selection is invalid", error_code="invalid_response"
            )
        return pending

    def complete_site_authorization(
        self, pending_id: str, actor_id: str, workspace_gid: str
    ) -> str:
        pending = self.pending_site_authorization(pending_id, actor_id)
        try:
            workspaces = pending.values.get("workspaces")
            assert isinstance(workspaces, list)
            selected = next(
                (
                    item
                    for item in workspaces
                    if isinstance(item, dict)
                    and isinstance(item.get("id"), str)
                    and item["id"] == workspace_gid
                ),
                None,
            )
            if not selected:
                raise AsanaIntegrationError(
                    "Asana workspace access changed; connect again",
                    error_code="workspace_mismatch",
                )
            values = {
                key: value
                for key, value in pending.values.items()
                if key
                in {
                    "access_token",
                    "refresh_token",
                    "scope",
                    "user_gid",
                    "display_name",
                }
                and isinstance(value, str)
            }
            expires_at = _as_utc(pending.values.get("access_expires_at"))
            integration_id = self.database.upsert_asana_workspace(
                str(selected["id"]), str(selected.get("name") or ""), actor_id
            )
            identity = self._identity(values)
            duplicate_identity = next(
                (
                    connection
                    for connection in self.database.list_asana_user_connections(
                        integration_id
                    )
                    if connection["asana_user_gid"] == identity["user_gid"]
                    and connection["user_id"] != actor_id
                ),
                None,
            )
            if duplicate_identity or self.database.asana_comment_identity_conflicts(
                integration_id, actor_id, identity["user_gid"]
            ):
                self.database.delete_unconfigured_asana_integration(integration_id)
                raise AsanaIntegrationError(
                    "Reconnect the same Asana account that owns existing comments",
                    error_code="identity_conflict",
                    retry_seconds=3600,
                )
            assert self.vault is not None
            previous: OpenedCredential | None = None
            try:
                previous = self.vault.open(integration_id, "asana")
            except IntegrationCredentialError:
                pass
            stored = False
            self.store_authorization(integration_id, values, expires_at)
            stored = True
            try:
                self.database.upsert_asana_user_connection(
                    integration_id,
                    actor_id,
                    identity["user_gid"],
                    identity["display_name"],
                    "site",
                )
            except Exception:
                if stored:
                    try:
                        if previous is None:
                            self.database.delete_integration_credentials(
                                integration_id, "asana"
                            )
                        else:
                            self.store_authorization(
                                integration_id,
                                dict(previous.values),
                                _as_utc(previous.access_expires_at),
                            )
                    except Exception:
                        LOGGER.exception("asana_authorization_rollback_failed")
                self.database.delete_unconfigured_asana_integration(integration_id)
                raise
            return integration_id
        finally:
            assert self.vault is not None
            try:
                self.vault.delete_pending(pending_id, actor_id, "asana")
            except IntegrationCredentialError:
                LOGGER.exception("asana_pending_authorization_delete_failed")

    def store_authorization(
        self,
        integration_id: str,
        values: dict[str, str],
        access_expires_at: datetime,
        *,
        subject_user_id: str | None = None,
    ) -> None:
        self._require_enabled()
        assert self.vault is not None
        try:
            self.vault.store(
                integration_id,
                "asana",
                values,
                access_expires_at=access_expires_at,
                subject_user_id=subject_user_id,
            )
        except IntegrationCredentialError as exc:
            raise AsanaIntegrationError(
                "Asana authorization could not be stored",
                error_code="credential_storage_failed",
            ) from exc

    def complete_member_authorization(
        self,
        integration_id: str,
        user_id: str,
        values: dict[str, str],
        access_expires_at: datetime,
    ) -> None:
        identity = self._identity(values)
        assert self.vault is not None
        previous: OpenedCredential | None = None
        try:
            previous = self.vault.open(integration_id, "asana", subject_user_id=user_id)
        except IntegrationCredentialError:
            pass
        self.store_authorization(
            integration_id,
            values,
            access_expires_at,
            subject_user_id=user_id,
        )
        try:
            self.database.upsert_asana_user_connection(
                integration_id,
                user_id,
                identity["user_gid"],
                identity["display_name"],
                "member",
            )
        except Exception:
            try:
                if previous is None:
                    self.database.delete_user_integration_credentials(
                        integration_id, user_id, "asana"
                    )
                else:
                    self.store_authorization(
                        integration_id,
                        dict(previous.values),
                        _as_utc(previous.access_expires_at),
                        subject_user_id=user_id,
                    )
            except Exception:
                LOGGER.exception("asana_member_authorization_rollback_failed")
            raise

    def _release_refresh(self, claim: ClaimedCredential) -> None:
        assert self.vault is not None
        try:
            self.vault.release_refresh(claim)
        except IntegrationCredentialError:
            LOGGER.exception("asana_refresh_claim_release_failed")

    def _refresh(self, claim: ClaimedCredential) -> str:
        assert self.vault is not None
        refresh_token = claim.credential.values.get("refresh_token")
        if not isinstance(refresh_token, str):
            self._release_refresh(claim)
            raise AsanaIntegrationError(
                "Asana refresh authorization is unavailable",
                error_code="reauthorization_required",
                retry_seconds=3600,
            )
        try:
            response = self._request(
                "POST",
                self.settings.asana_token_url,
                form_body={
                    "grant_type": "refresh_token",
                    "client_id": self.settings.asana_client_id,
                    "client_secret": self.settings.asana_client_secret,
                    "refresh_token": refresh_token,
                },
                allowed_error_statuses=frozenset({400}),
            )
            values, expires_at = self._token_values(
                response,
                require_refresh=False,
                prior=dict(claim.credential.values),
            )
            self.vault.replace_after_refresh(
                claim.credential.integration_id,
                "asana",
                claim.claim_token,
                claim.credential.revision,
                values,
                access_expires_at=expires_at,
                subject_user_id=claim.credential.subject_user_id,
            )
            return values["access_token"]
        except AsanaIntegrationError:
            self._release_refresh(claim)
            raise
        except IntegrationCredentialError as exc:
            self._release_refresh(claim)
            raise AsanaIntegrationError(
                "Asana authorization could not be rotated safely",
                error_code="credential_storage_failed",
                retry_seconds=300,
            ) from exc

    def _access_token(
        self, integration_id: str, subject_user_id: str | None = None
    ) -> str:
        self._require_enabled()
        assert self.vault is not None
        try:
            opened = self.vault.open(
                integration_id, "asana", subject_user_id=subject_user_id
            )
        except IntegrationCredentialError as exc:
            raise AsanaIntegrationError(
                "Asana authorization is unavailable",
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
                "asana",
                datetime.now(UTC),
                lease_seconds=90,
                subject_user_id=subject_user_id,
            )
        except IntegrationCredentialError as exc:
            raise AsanaIntegrationError(
                "Asana authorization is unavailable",
                error_code="credential_unavailable",
                retry_seconds=300,
            ) from exc
        if claim:
            return self._refresh(claim)
        for _attempt in range(10):
            self._sleep(0.1)
            current = self.vault.open(
                integration_id, "asana", subject_user_id=subject_user_id
            )
            current_expiry = _as_utc(current.access_expires_at)
            current_token = current.values.get("access_token")
            if (
                current.revision > opened.revision
                and isinstance(current_token, str)
                and current_expiry > datetime.now(UTC) + timedelta(seconds=30)
            ):
                return current_token
        raise AsanaIntegrationError(
            "Asana authorization refresh is already in progress",
            error_code="refresh_busy",
            retry_seconds=30,
        )

    def access_token(self, integration_id: str) -> str:
        return self._access_token(integration_id)

    def user_access_token(self, integration_id: str, user_id: str) -> str:
        connection = self.database.asana_user_connection(integration_id, user_id)
        if (
            not connection
            or not connection["enabled"]
            or not connection["user_enabled"]
            or not connection["integration_enabled"]
        ):
            raise AsanaIntegrationError(
                "Asana member authorization is unavailable",
                error_code="reauthorization_required",
                retry_seconds=3600,
            )
        if connection["credential_kind"] == "site":
            return self._access_token(integration_id)
        return self._access_token(integration_id, user_id)

    def _api_request(
        self, integration_id: str, method: str, endpoint: str
    ) -> httpx.Response:
        return self._api_url_request(
            integration_id, method, f"{self.settings.asana_api_url}{endpoint}"
        )

    def _api_url_request(
        self, integration_id: str, method: str, url: str
    ) -> httpx.Response:
        if not url.startswith(f"{self.settings.asana_api_url}/"):
            raise AsanaIntegrationError(
                "Asana API location is invalid", error_code="invalid_response"
            )
        token = self.access_token(integration_id)
        try:
            return self._request(method, url, token=token)
        except AsanaIntegrationError as exc:
            if exc.error_code != "authentication_failed":
                raise
        self.database.expire_integration_access(
            integration_id, "asana", datetime.now(UTC)
        )
        token = self.access_token(integration_id)
        return self._request(method, url, token=token)

    def _user_api_request(
        self,
        integration_id: str,
        user_id: str,
        method: str,
        endpoint: str,
        *,
        json_body: dict[str, Any] | None = None,
        allowed_error_statuses: frozenset[int] = frozenset(),
    ) -> httpx.Response:
        url = f"{self.settings.asana_api_url}{endpoint}"
        token = self.user_access_token(integration_id, user_id)
        try:
            return self._request(
                method,
                url,
                token=token,
                json_body=json_body,
                allowed_error_statuses=allowed_error_statuses,
            )
        except AsanaIntegrationError as exc:
            if exc.error_code != "authentication_failed":
                raise
        member = self.database.asana_user_connection(integration_id, user_id)
        if not member:
            raise AsanaIntegrationError(
                "Asana member authorization is unavailable",
                error_code="reauthorization_required",
                retry_seconds=3600,
            )
        if member["credential_kind"] == "site":
            self.database.expire_integration_access(
                integration_id, "asana", datetime.now(UTC)
            )
        else:
            self.database.expire_user_integration_access(
                integration_id, user_id, "asana", datetime.now(UTC)
            )
        token = self.user_access_token(integration_id, user_id)
        return self._request(
            method,
            url,
            token=token,
            json_body=json_body,
            allowed_error_statuses=allowed_error_statuses,
        )

    def list_projects(self, integration_id: str) -> list[dict[str, str]]:
        integration = self.database.get_asana_integration(integration_id)
        if not integration or not integration["enabled"]:
            raise AsanaIntegrationError(
                "Asana integration is unavailable", error_code="not_found"
            )
        workspace_gid = str(integration["provider_resource_key"])
        if not _GID.fullmatch(workspace_gid):
            raise AsanaIntegrationError(
                "Asana workspace identity is invalid", error_code="invalid_response"
            )
        raw = self._page_items(
            f"{self.settings.asana_api_url}/workspaces/{quote(workspace_gid, safe='')}"
            "/projects?archived=false&limit=100",
            maximum=MAX_ASANA_PROJECTS,
            request_page=lambda url: self._api_url_request(integration_id, "GET", url),
        )
        projects: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in raw:
            gid = str(item.get("gid") or "")
            value = item.get("name")
            name = (
                " ".join(value.replace("\x00", "").split())
                if isinstance(value, str)
                else ""
            )
            if not _GID.fullmatch(gid) or gid in seen or not name or len(name) > 255:
                raise AsanaIntegrationError(
                    "Asana returned an invalid project", error_code="invalid_response"
                )
            seen.add(gid)
            projects.append({"id": gid, "name": name})
        return projects

    @staticmethod
    def _parse_task(item: dict[str, Any]) -> dict[str, Any]:
        gid = str(item.get("gid") or "")
        name_value = item.get("name")
        name = name_value if isinstance(name_value, str) else ""
        notes_value = item.get("notes")
        notes = notes_value if isinstance(notes_value, str) else ""
        completed = item.get("completed")
        url_value = item.get("permalink_url")
        url = url_value if isinstance(url_value, str) else ""
        assignee = item.get("assignee")
        assignee_gid = (
            str(assignee.get("gid") or "") if isinstance(assignee, dict) else ""
        )
        try:
            num_subtasks = int(item.get("num_subtasks", 0))
        except (TypeError, ValueError) as exc:
            raise AsanaIntegrationError(
                "Asana returned an invalid task", error_code="invalid_response"
            ) from exc
        if (
            not _GID.fullmatch(gid)
            or not name
            or len(name) > 10_000
            or len(notes) > 100_000
            or completed not in {True, False}
            or len(url) > 1000
            or (assignee_gid and not _GID.fullmatch(assignee_gid))
            or not 0 <= num_subtasks <= MAX_ASANA_TASKS_PER_PROJECT
        ):
            raise AsanaIntegrationError(
                "Asana returned an invalid task", error_code="invalid_response"
            )
        return {
            "gid": gid,
            "name": name,
            "notes": notes,
            "completed": completed,
            "modified_at": _as_utc(item.get("modified_at")),
            "url": url,
            "assignee_gid": assignee_gid,
            "num_subtasks": num_subtasks,
        }

    def _task_page_url(self, container: str, *, subtasks: bool = False) -> str:
        fields = (
            "gid,name,notes,completed,modified_at,permalink_url,"
            "assignee.gid,num_subtasks"
        )
        path = (
            f"/tasks/{quote(container, safe='')}/subtasks"
            if subtasks
            else f"/projects/{quote(container, safe='')}/tasks"
        )
        query: dict[str, str | int] = {
            "limit": 100,
            "opt_fields": fields,
        }
        if not subtasks:
            query["completed_since"] = "1970-01-01T00:00:00.000Z"
        return f"{self.settings.asana_api_url}{path}?{urlencode(query)}"

    def _project_tasks(
        self,
        integration_id: str,
        external_project_id: str,
        renew: Callable[[], None],
    ) -> list[dict[str, Any]]:
        raw = self._page_items(
            self._task_page_url(external_project_id),
            maximum=MAX_ASANA_TASKS_PER_PROJECT,
            renew=renew,
            request_page=lambda url: self._api_url_request(integration_id, "GET", url),
        )
        parsed: dict[str, dict[str, Any]] = {}
        pending_subtasks: deque[str] = deque()
        for item in raw:
            task = self._parse_task(item)
            if task["gid"] in parsed:
                raise AsanaIntegrationError(
                    "Asana returned a duplicate task", error_code="invalid_response"
                )
            parsed[task["gid"]] = task
            if task["num_subtasks"]:
                pending_subtasks.append(task["gid"])
        examined_parents: set[str] = set()
        while pending_subtasks:
            parent_gid = pending_subtasks.popleft()
            if parent_gid in examined_parents:
                continue
            examined_parents.add(parent_gid)
            subtask_items = self._page_items(
                self._task_page_url(parent_gid, subtasks=True),
                maximum=MAX_ASANA_TASKS_PER_PROJECT,
                renew=renew,
                request_page=lambda url: self._api_url_request(
                    integration_id, "GET", url
                ),
            )
            for item in subtask_items:
                task = self._parse_task(item)
                if task["gid"] not in parsed:
                    parsed[task["gid"]] = task
                    if len(parsed) > MAX_ASANA_TASKS_PER_PROJECT:
                        raise AsanaIntegrationError(
                            "Asana project exceeds the configured task safety limit",
                            error_code="result_too_large",
                            retry_seconds=3600,
                        )
                    if task["num_subtasks"]:
                        pending_subtasks.append(task["gid"])
        return list(parsed.values())

    def _sync_claimed(
        self, integration: dict[str, Any], claim_token: str
    ) -> tuple[int, bool]:
        integration_id = str(integration["id"])
        mapping_count = int(integration.get("mapping_count") or 0)
        if not mapping_count:
            mapping_count = len(self.database.asana_project_mappings(integration_id))
        if mapping_count > MAX_ASANA_MAPPINGS:
            raise AsanaIntegrationError(
                "Asana mapping count exceeds the configured safety limit",
                error_code="result_too_large",
                retry_seconds=3600,
            )
        mappings = self.database.claim_asana_project_mappings(
            integration_id,
            claim_token,
            datetime.now(UTC),
            limit=ASANA_MAPPING_BATCH_SIZE,
        )
        total = 0
        try:
            for mapping in mappings:
                snapshot_at = self.database.integration_sync_snapshot_at()

                def renew(mapping: dict[str, Any] = mapping) -> None:
                    observed_at = datetime.now(UTC)
                    integration_owned = self.database.renew_asana_sync_claim(
                        integration_id, claim_token, observed_at
                    )
                    mapping_owned = self.database.renew_asana_mapping_claim(
                        integration_id,
                        str(mapping["external_project_id"]),
                        claim_token,
                        observed_at,
                    )
                    if not integration_owned or not mapping_owned:
                        raise AsanaIntegrationError(
                            "Asana synchronization ownership was lost",
                            error_code="sync_claim_lost",
                            retry_seconds=30,
                        )

                tasks = self._project_tasks(
                    integration_id, str(mapping["external_project_id"]), renew
                )
                seen_keys: list[str] = []
                for task in tasks:
                    renew()
                    task_id = self.database.apply_asana_task(
                        integration_id,
                        str(mapping["external_project_id"]),
                        task,
                        snapshot_at,
                    )
                    if task_id:
                        seen_keys.append(
                            f"asana:{mapping['external_project_id']}:{task['gid']}"
                        )
                        total += 1
                self.database.reconcile_asana_project_tasks(
                    integration_id,
                    str(mapping["external_project_id"]),
                    seen_keys,
                    snapshot_at,
                )
                if not self.database.mark_asana_mapping_synced(
                    integration_id,
                    str(mapping["external_project_id"]),
                    claim_token,
                    snapshot_at,
                ):
                    raise AsanaIntegrationError(
                        "Asana synchronization ownership was lost",
                        error_code="sync_claim_lost",
                        retry_seconds=30,
                    )
            has_more = self.database.has_due_asana_project_mappings(
                integration_id, claim_token
            )
            return total, has_more
        except Exception:
            self.database.release_asana_mapping_claims(
                integration_id, claim_token, datetime.now(UTC)
            )
            raise

    def sync_now(self, integration_id: str) -> int:
        self._require_enabled()
        observed_at = datetime.now(UTC)
        claim_token = str(uuid4())
        integration = self.database.claim_asana_integration(
            integration_id, claim_token, observed_at
        )
        if not integration:
            raise AsanaIntegrationError(
                "Asana synchronization is already running",
                error_code="sync_busy",
                retry_seconds=30,
            )
        try:
            count, has_more = self._sync_claimed(integration, claim_token)
            if has_more:
                self.database.mark_asana_sync_partial(
                    integration_id, claim_token, datetime.now(UTC)
                )
            else:
                self.database.mark_asana_sync_succeeded(
                    integration_id, claim_token, datetime.now(UTC)
                )
            return count
        except AsanaIntegrationError as exc:
            self.database.mark_asana_sync_failed(
                integration_id,
                claim_token,
                datetime.now(UTC),
                exc.error_code,
                retry_seconds=exc.retry_seconds,
            )
            raise
        except Exception:
            self.database.mark_asana_sync_failed(
                integration_id,
                claim_token,
                datetime.now(UTC),
                "internal_error",
                retry_seconds=300,
            )
            raise

    def sync_due(self) -> int:
        if not self.enabled:
            return 0
        observed_at = datetime.now(UTC)
        claim_token = str(uuid4())
        integrations = self.database.claim_due_asana_integrations(
            observed_at, claim_token
        )
        synchronized = 0
        for integration in integrations:
            integration_id = str(integration["id"])
            try:
                count, has_more = self._sync_claimed(integration, claim_token)
                synchronized += count
                if has_more:
                    self.database.mark_asana_sync_partial(
                        integration_id, claim_token, datetime.now(UTC)
                    )
                else:
                    self.database.mark_asana_sync_succeeded(
                        integration_id, claim_token, datetime.now(UTC)
                    )
            except AsanaIntegrationError as exc:
                self.database.mark_asana_sync_failed(
                    integration_id,
                    claim_token,
                    datetime.now(UTC),
                    exc.error_code,
                    retry_seconds=exc.retry_seconds,
                )
                LOGGER.warning(
                    "asana_sync_failed", extra={"error_code": exc.error_code}
                )
            except Exception:
                self.database.mark_asana_sync_failed(
                    integration_id,
                    claim_token,
                    datetime.now(UTC),
                    "internal_error",
                    retry_seconds=300,
                )
                LOGGER.exception("asana_sync_failed")
        return synchronized

    @staticmethod
    def _comment_date(value: object) -> date:
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        try:
            return date.fromisoformat(str(value))
        except ValueError as exc:
            raise AsanaIntegrationError(
                "Asana comment date is invalid", error_code="invalid_comment"
            ) from exc

    @staticmethod
    def _comment_text(export: dict[str, Any]) -> str:
        try:
            export_id = str(UUID(str(export["id"])))
            seconds = int(export["desired_seconds"])
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise AsanaIntegrationError(
                "Asana comment identity is invalid", error_code="invalid_comment"
            ) from exc
        if not 0 <= seconds <= 2_147_483_647:
            raise AsanaIntegrationError(
                "Asana comment duration is invalid", error_code="invalid_comment"
            )
        work_date = AsanaCloudService._comment_date(export.get("work_date"))
        marker = f"[dayfinch-export:{export_id}]"
        if seconds == 0:
            return f"Dayfinch tracked time removed for {work_date.isoformat()} UTC.\n{marker}"
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds_value = divmod(remainder, 60)
        duration = f"{hours:02d}:{minutes:02d}:{seconds_value:02d}"
        return f"Dayfinch tracked {duration} on {work_date.isoformat()} UTC.\n{marker}"

    def _renew_comment_claim(self, export: dict[str, Any]) -> None:
        claim_token = str(export.get("claim_token") or "")
        if not claim_token or not self.database.renew_asana_comment_export_claim(
            str(export.get("id") or ""),
            claim_token,
            datetime.now(UTC),
            lease_seconds=600,
        ):
            raise AsanaIntegrationError(
                "Asana comment delivery ownership was lost",
                error_code="comment_claim_lost",
                retry_seconds=30,
            )

    def _find_export_story(
        self,
        export: dict[str, Any],
        integration_id: str,
        user_id: str,
        task_gid: str,
    ) -> str | None:
        try:
            marker = f"[dayfinch-export:{str(UUID(str(export['id'])))}]"
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise AsanaIntegrationError(
                "Asana comment identity is invalid", error_code="invalid_comment"
            ) from exc
        offset = ""
        seen_offsets: set[str] = set()
        examined = 0
        matches: set[str] = set()
        while True:
            self._renew_comment_claim(export)
            query: dict[str, str | int] = {
                "limit": 100,
                "opt_fields": "gid,resource_subtype,text,created_by.gid",
            }
            if offset:
                query["offset"] = offset
            response = self._user_api_request(
                integration_id,
                user_id,
                "GET",
                f"/tasks/{quote(task_gid, safe='')}/stories?{urlencode(query)}",
            )
            payload = self._json(response)
            stories = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(stories, list) or any(
                not isinstance(story, dict) for story in stories
            ):
                raise AsanaIntegrationError(
                    "Asana returned an invalid story page",
                    error_code="invalid_response",
                )
            examined += len(stories)
            if examined > MAX_ASANA_STORIES_PER_TASK:
                raise AsanaIntegrationError(
                    "Asana story history exceeds the configured safety limit",
                    error_code="result_too_large",
                    retry_seconds=3600,
                )
            for story in stories:
                story_gid = str(story.get("gid") or "")
                created_by = story.get("created_by")
                text = story.get("text")
                if (
                    not _GID.fullmatch(story_gid)
                    or not isinstance(created_by, dict)
                    or not isinstance(text, str)
                    or len(text) > 100_000
                    or story.get("resource_subtype") != "comment_added"
                ):
                    raise AsanaIntegrationError(
                        "Asana returned an invalid story",
                        error_code="invalid_response",
                    )
                if created_by.get("gid") == export["asana_user_gid"] and marker in text:
                    matches.add(story_gid)
            next_page = payload.get("next_page")
            if next_page is None:
                break
            next_offset = (
                next_page.get("offset") if isinstance(next_page, dict) else None
            )
            if (
                not isinstance(next_offset, str)
                or not _OFFSET.fullmatch(next_offset)
                or next_offset in seen_offsets
            ):
                raise AsanaIntegrationError(
                    "Asana returned invalid story pagination",
                    error_code="invalid_response",
                )
            seen_offsets.add(next_offset)
            offset = next_offset
        if len(matches) > 1:
            raise AsanaIntegrationError(
                "Multiple Asana comments have the same Dayfinch identity",
                error_code="duplicate_comments",
                retry_seconds=3600,
            )
        return next(iter(matches)) if matches else None

    @staticmethod
    def _story_id(response: httpx.Response) -> str:
        payload = AsanaCloudService._json(response)
        story = payload.get("data") if isinstance(payload, dict) else None
        story_gid = str(story.get("gid") or "") if isinstance(story, dict) else ""
        if not _GID.fullmatch(story_gid):
            raise AsanaIntegrationError(
                "Asana returned an invalid comment", error_code="invalid_response"
            )
        return story_gid

    def _deliver_claimed_comment(
        self, export: dict[str, Any]
    ) -> tuple[str | None, int, datetime | None]:
        self._renew_comment_claim(export)
        integration_id = str(export["integration_id"])
        user_id = str(export["user_id"])
        task_gid = str(export["external_task_gid"])
        if not _GID.fullmatch(task_gid):
            raise AsanaIntegrationError(
                "Asana task identity is invalid", error_code="invalid_comment"
            )
        if export["asana_user_gid"] != export["connected_user_gid"]:
            raise AsanaIntegrationError(
                "Reconnect the Asana account that owns these comments",
                error_code="identity_conflict",
                retry_seconds=3600,
            )
        desired_seconds = int(export["desired_seconds"])
        desired_started = export.get("desired_started_at")
        desired_started = _as_utc(desired_started) if desired_seconds > 0 else None
        body = {"data": {"text": self._comment_text(export)}}
        provider_gid = export.get("provider_story_gid")
        if provider_gid is not None and not _GID.fullmatch(str(provider_gid)):
            raise AsanaIntegrationError(
                "Asana comment identity is invalid", error_code="invalid_comment"
            )
        if provider_gid:
            self._renew_comment_claim(export)
            response = self._user_api_request(
                integration_id,
                user_id,
                "PUT",
                f"/stories/{quote(str(provider_gid), safe='')}",
                json_body=body,
                allowed_error_statuses=frozenset({404}),
            )
            if response.status_code != 404:
                return self._story_id(response), desired_seconds, desired_started
        recovered_gid = self._find_export_story(
            export, integration_id, user_id, task_gid
        )
        if recovered_gid:
            self._renew_comment_claim(export)
            response = self._user_api_request(
                integration_id,
                user_id,
                "PUT",
                f"/stories/{quote(recovered_gid, safe='')}",
                json_body=body,
            )
            return self._story_id(response), desired_seconds, desired_started
        if desired_seconds == 0:
            return None, 0, None
        self._renew_comment_claim(export)
        response = self._user_api_request(
            integration_id,
            user_id,
            "POST",
            f"/tasks/{quote(task_gid, safe='')}/stories",
            json_body=body,
        )
        return self._story_id(response), desired_seconds, desired_started

    def sync_due_comments(self) -> int:
        if not self.enabled:
            return 0
        observed_at = datetime.now(UTC)
        self.database.stage_due_asana_comments(observed_at)
        delivered = 0
        while True:
            claim_token = str(uuid4())
            exports = self.database.claim_due_asana_comment_exports(
                datetime.now(UTC), claim_token, limit=25, lease_seconds=180
            )
            if not exports:
                break
            for export in exports:
                try:
                    provider_gid, seconds, started_at = self._deliver_claimed_comment(
                        export
                    )
                    saved = self.database.mark_asana_comment_export_succeeded(
                        str(export["id"]),
                        claim_token,
                        provider_gid,
                        seconds,
                        started_at,
                        datetime.now(UTC),
                    )
                    if saved:
                        delivered += 1
                    else:
                        LOGGER.warning("asana_comment_claim_lost")
                except AsanaIntegrationError as exc:
                    self.database.mark_asana_comment_export_failed(
                        str(export["id"]),
                        claim_token,
                        datetime.now(UTC),
                        exc.error_code,
                        exc.retry_seconds,
                    )
                    LOGGER.warning(
                        "asana_comment_sync_failed",
                        extra={"error_code": exc.error_code},
                    )
                except Exception:
                    self.database.mark_asana_comment_export_failed(
                        str(export["id"]),
                        claim_token,
                        datetime.now(UTC),
                        "internal_error",
                        300,
                    )
                    LOGGER.exception("asana_comment_sync_failed")
            if len(exports) < 25:
                break
        return delivered

    def revoke(self, values: dict[str, Any]) -> None:
        refresh_token = values.get("refresh_token")
        if not isinstance(refresh_token, str):
            return
        try:
            self._request(
                "POST",
                self.settings.asana_revoke_url,
                form_body={
                    "client_id": self.settings.asana_client_id,
                    "client_secret": self.settings.asana_client_secret,
                    "token": refresh_token,
                },
                allowed_error_statuses=frozenset({400}),
            )
        except AsanaIntegrationError:
            LOGGER.warning("asana_token_revocation_failed")

    def revoke_stored(
        self, integration_id: str, *, subject_user_id: str | None = None
    ) -> None:
        if not self.enabled or self.vault is None:
            return
        try:
            opened = self.vault.open(
                integration_id, "asana", subject_user_id=subject_user_id
            )
        except IntegrationCredentialError:
            return
        self.revoke(dict(opened.values))

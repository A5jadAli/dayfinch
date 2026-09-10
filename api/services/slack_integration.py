from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx

from ..config import Settings
from ..database import Database
from .integration_credentials import (
    ClaimedCredential,
    IntegrationCredentialError,
    IntegrationCredentialVault,
)

LOGGER = logging.getLogger("dayfinch-slack-integration")
SLACK_SCOPES = frozenset(
    {"chat:write", "chat:write.public", "channels:read", "groups:read", "users:read"}
)
MAX_SLACK_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_SLACK_CHANNELS = 10_000
MAX_SLACK_USERS = 10_000
_SLACK_ID = re.compile(r"^[A-Z][A-Z0-9]{1,30}$")
_CURSOR = re.compile(r"^[A-Za-z0-9._~+/=-]{1,4096}$")
_MESSAGE_TS = re.compile(r"^[0-9]{1,20}\.[0-9]{1,20}$")


class SlackIntegrationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "slack_unavailable",
        retry_seconds: int = 300,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.retry_seconds = min(max(int(retry_seconds), 1), 21_600)


class SlackCloudService:
    """Slack OAuth, bounded destination discovery, and durable notifications."""

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
        self.enabled = settings.slack_enabled
        if self.enabled and vault is None:
            raise SlackIntegrationError(
                "Slack credential encryption is not configured",
                error_code="not_configured",
            )
        self._sleep = sleep
        self._client = httpx.Client(
            timeout=httpx.Timeout(15.0, connect=5.0),
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": "Dayfinch-Slack-Integration"},
        )

    def close(self) -> None:
        self._client.close()

    def _require_enabled(self) -> None:
        if not self.enabled or self.vault is None:
            raise SlackIntegrationError(
                "Slack integration is not configured", error_code="not_configured"
            )

    def authorization_url(self, state: str) -> str:
        self._require_enabled()
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,200}", state):
            raise SlackIntegrationError("Slack OAuth state is invalid")
        query = urlencode(
            {
                "client_id": self.settings.slack_client_id,
                "redirect_uri": f"{self.settings.public_url}/integrations/slack/callback",
                "scope": ",".join(sorted(SLACK_SCOPES)),
                "state": state,
            }
        )
        separator = "&" if "?" in self.settings.slack_authorize_url else "?"
        return f"{self.settings.slack_authorize_url}{separator}{query}"

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        if len(response.content) > MAX_SLACK_RESPONSE_BYTES:
            raise SlackIntegrationError(
                "Slack returned an oversized response", error_code="invalid_response"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SlackIntegrationError(
                "Slack returned an invalid response", error_code="invalid_response"
            ) from exc
        if not isinstance(payload, dict):
            raise SlackIntegrationError(
                "Slack returned an invalid response", error_code="invalid_response"
            )
        return payload

    def _request(
        self,
        method: str,
        url: str,
        *,
        token: str = "",
        params: dict[str, str | int] | None = None,
        form_body: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        attempts = 3 if method.upper() in {"GET", "HEAD"} else 1
        response: httpx.Response | None = None
        for attempt in range(attempts):
            try:
                with self._client.stream(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    data=form_body,
                    json=json_body,
                ) as streamed:
                    declared = streamed.headers.get("Content-Length", "")
                    if declared.isdigit() and int(declared) > MAX_SLACK_RESPONSE_BYTES:
                        raise SlackIntegrationError(
                            "Slack returned an oversized response",
                            error_code="invalid_response",
                        )
                    body = bytearray()
                    for chunk in streamed.iter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_SLACK_RESPONSE_BYTES:
                            raise SlackIntegrationError(
                                "Slack returned an oversized response",
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
                    raise SlackIntegrationError(
                        "Slack could not be reached", error_code="network_error"
                    ) from exc
            else:
                if response.status_code < 500:
                    break
                if attempt == attempts - 1:
                    raise SlackIntegrationError(
                        "Slack is temporarily unavailable", error_code="provider_5xx"
                    )
            self._sleep(0.25 * (2**attempt))
        assert response is not None
        if response.status_code == 429:
            retry_header = response.headers.get("Retry-After", "")
            retry_seconds = int(retry_header) if retry_header.isdigit() else 60
            raise SlackIntegrationError(
                "Slack rate limit reached; delivery will retry later",
                error_code="rate_limited",
                retry_seconds=retry_seconds,
            )
        if response.status_code in {401, 403}:
            raise SlackIntegrationError(
                "Slack rejected the authorization",
                error_code="authentication_failed",
                retry_seconds=1800,
            )
        if response.is_error:
            raise SlackIntegrationError(
                "Slack rejected the integration request",
                error_code="provider_rejected",
            )
        return response

    def _checked_payload(self, response: httpx.Response) -> dict[str, Any]:
        payload = self._json(response)
        if payload.get("ok") is True:
            return payload
        error = payload.get("error")
        error_name = error if isinstance(error, str) and len(error) <= 100 else ""
        if error_name in {
            "invalid_auth",
            "not_authed",
            "token_expired",
            "token_revoked",
            "account_inactive",
        }:
            raise SlackIntegrationError(
                "Slack authorization expired; reconnect the workspace",
                error_code="authentication_failed",
                retry_seconds=1800,
            )
        if error_name in {
            "missing_scope",
            "not_in_channel",
            "channel_not_found",
            "no_permission",
            "restricted_action",
            "ekm_access_denied",
        }:
            raise SlackIntegrationError(
                "The Slack app cannot post to this destination",
                error_code="permission_denied",
                retry_seconds=3600,
            )
        if error_name == "ratelimited":
            raise SlackIntegrationError(
                "Slack rate limit reached; delivery will retry later",
                error_code="rate_limited",
                retry_seconds=60,
            )
        if error_name in {"fatal_error", "internal_error", "service_unavailable"}:
            raise SlackIntegrationError(
                "Slack is temporarily unavailable",
                error_code="provider_5xx",
                retry_seconds=300,
            )
        raise SlackIntegrationError(
            "Slack rejected the integration request", error_code="provider_rejected"
        )

    @staticmethod
    def _clean_name(value: object, fallback: str) -> str:
        cleaned = (
            " ".join(value.replace("\x00", "").split())
            if isinstance(value, str)
            else ""
        )
        if not cleaned:
            cleaned = fallback
        if len(cleaned) > 255:
            raise SlackIntegrationError(
                "Slack returned an invalid display name", error_code="invalid_response"
            )
        return cleaned

    def _token_values(
        self, payload: dict[str, Any], prior: dict[str, Any] | None = None
    ) -> tuple[dict[str, str], datetime | None]:
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        scope = payload.get("scope")
        token_type = str(payload.get("token_type") or "bot").lower()
        if refresh_token is None and prior:
            refresh_token = prior.get("refresh_token")
        if scope is None and prior:
            scope = prior.get("scope")
        if (
            not isinstance(access_token, str)
            or not 1 <= len(access_token) <= 8192
            or token_type != "bot"
            or not isinstance(scope, str)
            or len(scope) > 4096
        ):
            raise SlackIntegrationError(
                "Slack returned an invalid OAuth response",
                error_code="invalid_response",
            )
        granted = {item for item in re.split(r"[\s,]+", scope) if item}
        if not SLACK_SCOPES.issubset(granted):
            raise SlackIntegrationError(
                "Slack did not grant the required bot scopes",
                error_code="insufficient_scope",
                retry_seconds=3600,
            )
        team = payload.get("team")
        team_id = str(team.get("id") or "") if isinstance(team, dict) else ""
        team_name_value = team.get("name") if isinstance(team, dict) else None
        bot_user_id = str(payload.get("bot_user_id") or "")
        if prior:
            team_id = team_id or str(prior.get("team_id") or "")
            team_name_value = team_name_value or prior.get("team_name")
            bot_user_id = bot_user_id or str(prior.get("bot_user_id") or "")
        team_name = self._clean_name(team_name_value, "Slack workspace")
        if not _SLACK_ID.fullmatch(team_id) or not _SLACK_ID.fullmatch(bot_user_id):
            raise SlackIntegrationError(
                "Slack returned an invalid workspace identity",
                error_code="invalid_response",
            )
        expires_at: datetime | None = None
        if refresh_token is not None or payload.get("expires_in") is not None:
            try:
                expires_in = int(payload.get("expires_in"))
            except (TypeError, ValueError) as exc:
                raise SlackIntegrationError(
                    "Slack returned an invalid rotating token",
                    error_code="invalid_response",
                ) from exc
            if (
                not isinstance(refresh_token, str)
                or not 1 <= len(refresh_token) <= 8192
                or not 60 <= expires_in <= 86_400
            ):
                raise SlackIntegrationError(
                    "Slack returned an invalid rotating token",
                    error_code="invalid_response",
                )
            expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
        values = {
            "access_token": access_token,
            "scope": ",".join(sorted(granted)),
            "team_id": team_id,
            "team_name": team_name,
            "bot_user_id": bot_user_id,
        }
        if isinstance(refresh_token, str):
            values["refresh_token"] = refresh_token
        return values, expires_at

    def exchange_code(self, code: str) -> tuple[dict[str, str], datetime | None]:
        self._require_enabled()
        if not code or len(code) > 4096 or any(ord(char) < 32 for char in code):
            raise SlackIntegrationError("Slack authorization response is invalid")
        response = self._request(
            "POST",
            self.settings.slack_token_url,
            form_body={
                "client_id": self.settings.slack_client_id,
                "client_secret": self.settings.slack_client_secret,
                "code": code,
                "redirect_uri": f"{self.settings.public_url}/integrations/slack/callback",
            },
        )
        return self._token_values(self._checked_payload(response))

    def complete_authorization(
        self,
        actor_id: str,
        values: dict[str, str],
        access_expires_at: datetime | None,
    ) -> str:
        self._require_enabled()
        assert self.vault is not None
        previous = next(
            (
                integration
                for integration in self.database.list_slack_integrations()
                if integration["provider_resource_key"] == values.get("team_id", "")
            ),
            None,
        )
        integration_id = self.database.upsert_slack_workspace(
            values.get("team_id", ""),
            values.get("team_name", ""),
            values.get("bot_user_id", ""),
            actor_id,
        )
        try:
            self.vault.store(
                integration_id,
                "slack",
                values,
                access_expires_at=access_expires_at,
            )
        except IntegrationCredentialError as exc:
            try:
                if previous is None:
                    self.database.delete_unconfigured_slack_integration(integration_id)
                else:
                    self.database.restore_slack_workspace_after_failed_authorization(
                        integration_id, previous
                    )
            except Exception:
                LOGGER.exception("slack_authorization_rollback_failed")
            raise SlackIntegrationError(
                "Slack authorization could not be stored",
                error_code="credential_storage_failed",
            ) from exc
        return integration_id

    def _release_refresh(self, claim: ClaimedCredential) -> None:
        assert self.vault is not None
        try:
            self.vault.release_refresh(claim)
        except IntegrationCredentialError:
            LOGGER.exception("slack_refresh_claim_release_failed")

    def _refresh(self, claim: ClaimedCredential) -> str:
        assert self.vault is not None
        refresh_token = claim.credential.values.get("refresh_token")
        if not isinstance(refresh_token, str):
            self._release_refresh(claim)
            raise SlackIntegrationError(
                "Slack authorization expired; reconnect the workspace",
                error_code="reauthorization_required",
                retry_seconds=3600,
            )
        try:
            response = self._request(
                "POST",
                self.settings.slack_token_url,
                form_body={
                    "client_id": self.settings.slack_client_id,
                    "client_secret": self.settings.slack_client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            )
            values, expires_at = self._token_values(
                self._checked_payload(response), dict(claim.credential.values)
            )
            assert expires_at is not None
            self.vault.replace_after_refresh(
                claim.credential.integration_id,
                "slack",
                claim.claim_token,
                claim.credential.revision,
                values,
                access_expires_at=expires_at,
            )
            return values["access_token"]
        except SlackIntegrationError:
            self._release_refresh(claim)
            raise
        except IntegrationCredentialError as exc:
            self._release_refresh(claim)
            raise SlackIntegrationError(
                "Slack authorization could not be rotated safely",
                error_code="credential_storage_failed",
            ) from exc

    def access_token(self, integration_id: str) -> str:
        self._require_enabled()
        assert self.vault is not None
        try:
            opened = self.vault.open(integration_id, "slack")
        except IntegrationCredentialError as exc:
            raise SlackIntegrationError(
                "Slack authorization is unavailable",
                error_code="credential_unavailable",
                retry_seconds=1800,
            ) from exc
        token = opened.values.get("access_token")
        if not isinstance(token, str):
            raise SlackIntegrationError(
                "Slack authorization is invalid", error_code="invalid_response"
            )
        if opened.access_expires_at is None:
            return token
        try:
            expires_at = datetime.fromisoformat(
                str(opened.access_expires_at).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise SlackIntegrationError(
                "Slack authorization is invalid", error_code="invalid_response"
            ) from exc
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at.astimezone(UTC) > datetime.now(UTC) + timedelta(seconds=60):
            return token
        try:
            claim = self.vault.claim_for_refresh(
                integration_id, "slack", datetime.now(UTC), lease_seconds=90
            )
        except IntegrationCredentialError as exc:
            raise SlackIntegrationError(
                "Slack authorization is unavailable",
                error_code="credential_unavailable",
            ) from exc
        if claim:
            return self._refresh(claim)
        for _attempt in range(10):
            self._sleep(0.1)
            current = self.vault.open(integration_id, "slack")
            if current.revision > opened.revision:
                current_token = current.values.get("access_token")
                if isinstance(current_token, str):
                    return current_token
        raise SlackIntegrationError(
            "Slack authorization refresh is already in progress",
            error_code="refresh_busy",
            retry_seconds=30,
        )

    def _workspace_call(
        self,
        integration_id: str,
        method: str,
        *,
        http_method: str = "GET",
        params: dict[str, str | int] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.settings.slack_api_url}/{method}"
        token = self.access_token(integration_id)
        try:
            return self._checked_payload(
                self._request(
                    http_method,
                    url,
                    token=token,
                    params=params,
                    json_body=json_body,
                )
            )
        except SlackIntegrationError as exc:
            if exc.error_code != "authentication_failed":
                raise
        self.database.expire_integration_access(
            integration_id, "slack", datetime.now(UTC)
        )
        token = self.access_token(integration_id)
        return self._checked_payload(
            self._request(
                http_method,
                url,
                token=token,
                params=params,
                json_body=json_body,
            )
        )

    def _cursor_items(
        self,
        integration_id: str,
        method: str,
        *,
        maximum: int,
        params: dict[str, str | int],
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor = ""
        seen: set[str] = set()
        while True:
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            payload = self._workspace_call(integration_id, method, params=page_params)
            page = payload.get(
                "channels" if method == "conversations.list" else "members"
            )
            if not isinstance(page, list) or any(
                not isinstance(item, dict) for item in page
            ):
                raise SlackIntegrationError(
                    "Slack returned an invalid result page",
                    error_code="invalid_response",
                )
            items.extend(page)
            if len(items) > maximum:
                raise SlackIntegrationError(
                    "Slack result exceeds the configured safety limit",
                    error_code="result_too_large",
                    retry_seconds=3600,
                )
            metadata = payload.get("response_metadata")
            next_cursor = (
                metadata.get("next_cursor") if isinstance(metadata, dict) else ""
            )
            if next_cursor in {None, ""}:
                break
            if (
                not isinstance(next_cursor, str)
                or not _CURSOR.fullmatch(next_cursor)
                or next_cursor in seen
            ):
                raise SlackIntegrationError(
                    "Slack returned invalid pagination", error_code="invalid_response"
                )
            seen.add(next_cursor)
            cursor = next_cursor
        return items

    def list_destinations(self, integration_id: str) -> list[dict[str, str]]:
        integration = self.database.get_slack_integration(integration_id)
        if not integration or not integration["enabled"]:
            raise SlackIntegrationError(
                "Slack integration is unavailable", error_code="not_found"
            )
        channels = self._cursor_items(
            integration_id,
            "conversations.list",
            maximum=MAX_SLACK_CHANNELS,
            params={
                "exclude_archived": "true",
                "types": "public_channel,private_channel",
                "limit": 200,
            },
        )
        users = self._cursor_items(
            integration_id,
            "users.list",
            maximum=MAX_SLACK_USERS,
            params={"limit": 200},
        )
        destinations: list[dict[str, str]] = []
        seen: set[str] = set()
        for channel in channels:
            target_id = str(channel.get("id") or "")
            if channel.get("is_archived") is True:
                continue
            if not _SLACK_ID.fullmatch(target_id) or target_id in seen:
                raise SlackIntegrationError(
                    "Slack returned an invalid channel", error_code="invalid_response"
                )
            seen.add(target_id)
            name = self._clean_name(channel.get("name"), "Slack channel")
            destinations.append(
                {"id": target_id, "kind": "channel", "name": f"#{name}"}
            )
        for member in users:
            target_id = str(member.get("id") or "")
            if (
                member.get("deleted") is True
                or member.get("is_bot") is True
                or member.get("is_app_user") is True
                or target_id == integration["account_type"]
            ):
                continue
            if not _SLACK_ID.fullmatch(target_id) or target_id in seen:
                raise SlackIntegrationError(
                    "Slack returned an invalid user", error_code="invalid_response"
                )
            seen.add(target_id)
            profile = member.get("profile")
            display_name = (
                profile.get("display_name") if isinstance(profile, dict) else None
            )
            real_name = profile.get("real_name") if isinstance(profile, dict) else None
            name = self._clean_name(
                display_name or real_name or member.get("name"), "Slack user"
            )
            destinations.append({"id": target_id, "kind": "user", "name": f"@{name}"})
        destinations.sort(
            key=lambda item: (item["kind"], item["name"].lower(), item["id"])
        )
        return destinations

    def add_destination(
        self, integration_id: str, target_id: str, target_kind: str
    ) -> str:
        available = self.list_destinations(integration_id)
        target = next(
            (
                item
                for item in available
                if item["id"] == target_id and item["kind"] == target_kind
            ),
            None,
        )
        if not target:
            raise SlackIntegrationError(
                "Slack destination access changed; reload",
                error_code="destination_mismatch",
            )
        return self.database.set_slack_destination(
            integration_id, target["id"], target["kind"], target["name"]
        )

    @staticmethod
    def _message_text(message: dict[str, Any]) -> str:
        event_type = str(message.get("event_type") or "")
        user_name = " ".join(str(message.get("user_name") or "A team member").split())
        project_name = " ".join(
            str(message.get("project_name") or "an unavailable project").split()
        )
        task_name = " ".join(str(message.get("task_name") or "").split())
        if event_type == "timer_started":
            work = f" on {task_name}" if task_name else ""
            text = f"{user_name} started tracking{work} in {project_name}."
        elif event_type == "timer_stopped":
            try:
                seconds = max(0, int(message.get("tracked_seconds") or 0))
            except (TypeError, ValueError) as exc:
                raise SlackIntegrationError(
                    "Slack notification duration is invalid",
                    error_code="invalid_message",
                ) from exc
            hours, remainder = divmod(seconds, 3600)
            minutes, second_value = divmod(remainder, 60)
            work = f" on {task_name}" if task_name else ""
            text = (
                f"{user_name} stopped tracking{work} in {project_name} after "
                f"{hours:02d}:{minutes:02d}:{second_value:02d}."
            )
        elif event_type == "todo_completed":
            todo_name = " ".join(str(message.get("todo_name") or "a to-do").split())
            text = f"{user_name} completed {todo_name} in {project_name}."
        else:
            raise SlackIntegrationError(
                "Slack notification type is invalid", error_code="invalid_message"
            )
        return text[:4000]

    @staticmethod
    def _retry_delay(message_id: str, base_seconds: int, attempt_count: object) -> int:
        try:
            attempts = min(max(int(attempt_count), 0), 6)
            identity = UUID(message_id).int
        except (TypeError, ValueError):
            attempts = 0
            identity = 0
        delay = min(max(int(base_seconds), 1) * (2**attempts), 21_600)
        jitter_range = max(1, delay // 5)
        return min(delay + identity % jitter_range, 21_600)

    def deliver_due(self) -> int:
        if not self.enabled:
            return 0
        claim_token = str(uuid4())
        messages = self.database.claim_due_slack_messages(
            datetime.now(UTC), claim_token, limit=25, lease_seconds=180
        )
        delivered = 0
        for message in messages:
            message_id = str(message["id"])
            try:
                UUID(message_id)
                if not self.database.renew_slack_message_claim(
                    message_id, claim_token, datetime.now(UTC)
                ):
                    raise SlackIntegrationError(
                        "Slack notification ownership was lost",
                        error_code="delivery_claim_lost",
                        retry_seconds=30,
                    )
                payload = self._workspace_call(
                    str(message["integration_id"]),
                    "chat.postMessage",
                    http_method="POST",
                    json_body={
                        "channel": str(message["slack_target_id"]),
                        "text": self._message_text(message),
                        "mrkdwn": False,
                        "unfurl_links": False,
                        "unfurl_media": False,
                        "client_msg_id": message_id,
                    },
                )
                provider_ts = str(payload.get("ts") or "")
                if not _MESSAGE_TS.fullmatch(provider_ts):
                    raise SlackIntegrationError(
                        "Slack returned an invalid message",
                        error_code="invalid_response",
                    )
                if self.database.mark_slack_message_succeeded(
                    message_id,
                    claim_token,
                    provider_ts,
                    datetime.now(UTC),
                ):
                    delivered += 1
            except SlackIntegrationError as exc:
                self.database.mark_slack_message_failed(
                    message_id,
                    claim_token,
                    datetime.now(UTC),
                    exc.error_code,
                    self._retry_delay(
                        message_id, exc.retry_seconds, message.get("attempt_count")
                    ),
                )
                LOGGER.warning(
                    "slack_notification_failed", extra={"error_code": exc.error_code}
                )
            except Exception:
                self.database.mark_slack_message_failed(
                    message_id,
                    claim_token,
                    datetime.now(UTC),
                    "internal_error",
                    self._retry_delay(message_id, 300, message.get("attempt_count")),
                )
                LOGGER.exception("slack_notification_failed")
        return delivered

    def revoke_stored(self, integration_id: str) -> None:
        if self.vault is None:
            return
        try:
            opened = self.vault.open(integration_id, "slack")
            token = opened.values.get("access_token")
            if isinstance(token, str):
                self._checked_payload(
                    self._request(
                        "POST",
                        f"{self.settings.slack_api_url}/auth.revoke",
                        token=token,
                    )
                )
        except (IntegrationCredentialError, SlackIntegrationError):
            LOGGER.warning("slack_token_revocation_failed")

from __future__ import annotations

import asyncio
import hmac
import json
import re
import secrets
import time
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from ..services.asana_integration import AsanaIntegrationError
from ..services.github_integration import (
    MAX_GITHUB_WEBHOOK_BYTES,
    GitHubIntegrationError,
    oauth_state_values,
)
from ..services.jira_integration import JiraIntegrationError
from ..services.slack_integration import SlackIntegrationError

router = APIRouter(prefix="/integrations", tags=["integrations"])


def _github_enabled(request: Request) -> None:
    if not request.app.state.github.enabled:
        raise HTTPException(status_code=404, detail="GitHub integration is unavailable")


def _jira_enabled(request: Request) -> None:
    if not request.app.state.jira.enabled:
        raise HTTPException(status_code=404, detail="Jira integration is unavailable")


def _asana_enabled(request: Request) -> None:
    if not request.app.state.asana.enabled:
        raise HTTPException(status_code=404, detail="Asana integration is unavailable")


def _slack_enabled(request: Request) -> None:
    if not request.app.state.slack.enabled:
        raise HTTPException(status_code=404, detail="Slack integration is unavailable")


@router.get("/slack/connect")
def slack_connect(request: Request):
    actor = request.app.state.web.require_owner(request)
    _slack_enabled(request)
    state_token = secrets.token_urlsafe(32)
    request.session["slack_oauth_pending"] = {
        "actor_id": actor["id"],
        "state": state_token,
        "issued_at": int(time.time()),
    }
    return RedirectResponse(
        request.app.state.slack.authorization_url(state_token),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/slack/callback")
async def slack_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
):
    actor = request.app.state.web.require_owner(request)
    _slack_enabled(request)
    pending = request.session.pop("slack_oauth_pending", None)
    now = int(time.time())
    if (
        not isinstance(pending, dict)
        or pending.get("actor_id") != actor["id"]
        or not isinstance(pending.get("state"), str)
        or not state
        or not hmac.compare_digest(pending["state"], state)
        or not isinstance(pending.get("issued_at"), int)
        or now - pending["issued_at"] not in range(0, 601)
    ):
        raise HTTPException(status_code=403, detail="Invalid or expired Slack state")
    if error:
        request.app.state.database.add_audit_event(
            actor["id"], "slack.connection_cancelled", "integration", None
        )
        raise HTTPException(status_code=422, detail="Slack authorization was cancelled")
    try:
        values, expires_at = await asyncio.to_thread(
            request.app.state.slack.exchange_code, code
        )
        integration_id = await asyncio.to_thread(
            request.app.state.slack.complete_authorization,
            actor["id"],
            values,
            expires_at,
        )
    except (SlackIntegrationError, ValueError) as exc:
        error_code = (
            exc.error_code
            if isinstance(exc, SlackIntegrationError)
            else "identity_conflict"
        )
        request.app.state.database.add_audit_event(
            actor["id"],
            "slack.connection_failed",
            "integration",
            None,
            f"error_code={error_code}",
        )
        response_status = (
            503
            if error_code
            in {
                "network_error",
                "provider_5xx",
                "rate_limited",
                "credential_storage_failed",
            }
            else 422
        )
        raise HTTPException(status_code=response_status, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"], "slack.connected", "integration", integration_id
    )
    return RedirectResponse(
        f"/integrations/slack/{integration_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/slack/{integration_id}", response_class=HTMLResponse)
async def slack_integration_page(request: Request, integration_id: str):
    request.app.state.web.require_owner(request)
    _slack_enabled(request)
    database = request.app.state.database
    integration = database.get_slack_integration(integration_id)
    if not integration:
        raise HTTPException(status_code=404, detail="Slack integration not found")
    available: list[dict] = []
    provider_error = ""
    if integration["enabled"]:
        try:
            available = await asyncio.to_thread(
                request.app.state.slack.list_destinations, integration_id
            )
        except SlackIntegrationError as exc:
            provider_error = str(exc)
    configured_ids = {
        item["slack_target_id"]
        for item in database.slack_destinations(integration_id)
        if item["enabled"]
    }
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="slack_integration.html",
        context=request.app.state.web.page_context(
            request,
            integration=integration,
            available_destinations=[
                item for item in available if item["id"] not in configured_ids
            ],
            destinations=database.slack_destinations(integration_id),
            notification_users=database.slack_notification_users(integration_id),
            provider_error=provider_error,
        ),
    )


@router.post("/slack/{integration_id}/destinations")
async def add_slack_destination(
    request: Request,
    integration_id: str,
    target: Annotated[str, Form(min_length=3, max_length=80)],
    csrf: Annotated[str, Form()],
):
    actor = request.app.state.web.require_owner(request)
    request.app.state.web.require_csrf(request, csrf)
    _slack_enabled(request)
    try:
        target_kind, target_id = target.split(":", 1)
        destination_id = await asyncio.to_thread(
            request.app.state.slack.add_destination,
            integration_id,
            target_id,
            target_kind,
        )
    except (SlackIntegrationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"],
        "slack.destination_added",
        "integration",
        integration_id,
        f"destination_id={destination_id}",
    )
    return RedirectResponse(
        f"/integrations/slack/{integration_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/slack/{integration_id}/destinations/{destination_id}/delete")
def delete_slack_destination(
    request: Request,
    integration_id: str,
    destination_id: str,
    csrf: Annotated[str, Form()],
):
    actor = request.app.state.web.require_owner(request)
    request.app.state.web.require_csrf(request, csrf)
    _slack_enabled(request)
    if not request.app.state.database.remove_slack_destination(
        integration_id, destination_id
    ):
        raise HTTPException(status_code=404, detail="Slack destination not found")
    request.app.state.database.add_audit_event(
        actor["id"],
        "slack.destination_removed",
        "integration",
        integration_id,
        f"destination_id={destination_id}",
    )
    return RedirectResponse(
        f"/integrations/slack/{integration_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/slack/{integration_id}/defaults")
def update_slack_defaults(
    request: Request,
    integration_id: str,
    csrf: Annotated[str, Form()],
    timer_events: Annotated[str | None, Form()] = None,
    todo_events: Annotated[str | None, Form()] = None,
):
    actor = request.app.state.web.require_owner(request)
    request.app.state.web.require_csrf(request, csrf)
    _slack_enabled(request)
    try:
        request.app.state.database.set_slack_notification_defaults(
            integration_id, timer_events == "on", todo_events == "on"
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"], "slack.defaults_updated", "integration", integration_id
    )
    return RedirectResponse(
        f"/integrations/slack/{integration_id}", status_code=status.HTTP_303_SEE_OTHER
    )


def _slack_rule_value(value: str) -> bool | None:
    if value == "inherit":
        return None
    if value in {"on", "off"}:
        return value == "on"
    raise ValueError("Choose a valid Slack notification rule")


@router.post("/slack/{integration_id}/users/{user_id}")
def update_slack_user_rule(
    request: Request,
    integration_id: str,
    user_id: str,
    timer_events: Annotated[str, Form()],
    todo_events: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
):
    actor = request.app.state.web.require_owner(request)
    request.app.state.web.require_csrf(request, csrf)
    _slack_enabled(request)
    try:
        request.app.state.database.set_slack_user_notification_rule(
            integration_id,
            user_id,
            _slack_rule_value(timer_events),
            _slack_rule_value(todo_events),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"],
        "slack.user_rule_updated",
        "integration",
        integration_id,
        f"user_id={user_id}",
    )
    return RedirectResponse(
        f"/integrations/slack/{integration_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/slack/{integration_id}/disconnect")
async def disconnect_slack_integration(
    request: Request,
    integration_id: str,
    csrf: Annotated[str, Form()],
):
    actor = request.app.state.web.require_owner(request)
    request.app.state.web.require_csrf(request, csrf)
    _slack_enabled(request)
    await asyncio.to_thread(request.app.state.slack.revoke_stored, integration_id)
    try:
        request.app.state.database.disconnect_slack_integration(integration_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"], "slack.disconnected", "integration", integration_id
    )
    return RedirectResponse("/settings", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/asana/connect")
def asana_connect(request: Request):
    actor = request.app.state.web.require_owner(request)
    _asana_enabled(request)
    state_token, verifier, challenge = oauth_state_values()
    request.session["asana_oauth_pending"] = {
        "actor_id": actor["id"],
        "state": state_token,
        "verifier": verifier,
        "issued_at": int(time.time()),
        "purpose": "site",
    }
    return RedirectResponse(
        request.app.state.asana.authorization_url(state_token, challenge),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/asana/callback")
async def asana_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
):
    actor = request.app.state.web.require_worker(request)
    _asana_enabled(request)
    pending = request.session.pop("asana_oauth_pending", None)
    now = int(time.time())
    if (
        not isinstance(pending, dict)
        or pending.get("actor_id") != actor["id"]
        or not isinstance(pending.get("state"), str)
        or not state
        or not hmac.compare_digest(pending["state"], state)
        or not isinstance(pending.get("verifier"), str)
        or not isinstance(pending.get("issued_at"), int)
        or now - pending["issued_at"] not in range(0, 601)
    ):
        raise HTTPException(status_code=403, detail="Invalid or expired Asana state")
    if error:
        request.app.state.database.add_audit_event(
            actor["id"], "asana.connection_cancelled", "integration", None
        )
        raise HTTPException(status_code=422, detail="Asana authorization was cancelled")
    asana = request.app.state.asana
    try:
        values, expires_at = await asyncio.to_thread(
            asana.exchange_code, code, pending["verifier"]
        )
        workspaces = await asyncio.to_thread(
            asana.workspaces_with_token, values["access_token"]
        )
        purpose = pending.get("purpose")
        if purpose == "member":
            integration_id = str(pending.get("integration_id") or "")
            integration = request.app.state.database.get_asana_integration(
                integration_id
            )
            if not integration or not integration["enabled"]:
                raise AsanaIntegrationError(
                    "Asana integration is unavailable", error_code="not_found"
                )
            if not any(
                item["id"] == integration["provider_resource_key"]
                for item in workspaces
            ):
                raise AsanaIntegrationError(
                    "Authorize an Asana account in the connected workspace",
                    error_code="workspace_mismatch",
                    retry_seconds=3600,
                )
            asana.complete_member_authorization(
                integration_id,
                actor["id"],
                values,
                expires_at,
            )
            request.app.state.database.add_audit_event(
                actor["id"], "asana.user_connected", "integration", integration_id
            )
            return RedirectResponse(
                f"/integrations/asana/{integration_id}/account",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        if purpose != "site":
            raise AsanaIntegrationError(
                "Asana authorization purpose is invalid", error_code="invalid_state"
            )
        request.app.state.web.require_owner(request)
        pending_id = asana.store_pending_site_authorization(
            actor["id"], values, expires_at, workspaces
        )
    except (AsanaIntegrationError, ValueError) as exc:
        error_code = (
            exc.error_code
            if isinstance(exc, AsanaIntegrationError)
            else "identity_conflict"
        )
        request.app.state.database.add_audit_event(
            actor["id"],
            "asana.connection_failed",
            "integration",
            None,
            f"error_code={error_code}",
        )
        response_status = (
            503
            if error_code
            in {
                "network_error",
                "provider_5xx",
                "rate_limited",
                "credential_storage_failed",
            }
            else 422
        )
        raise HTTPException(status_code=response_status, detail=str(exc)) from exc
    return RedirectResponse(
        f"/integrations/asana/select?pending_id={pending_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/asana/select", response_class=HTMLResponse)
def select_asana_workspace_page(request: Request, pending_id: str):
    actor = request.app.state.web.require_owner(request)
    _asana_enabled(request)
    try:
        pending = request.app.state.asana.pending_site_authorization(
            pending_id, actor["id"]
        )
    except AsanaIntegrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="asana_workspace_select.html",
        context=request.app.state.web.page_context(
            request,
            pending_id=pending_id,
            workspaces=pending.values["workspaces"],
        ),
    )


@router.post("/asana/select")
def select_asana_workspace(
    request: Request,
    pending_id: Annotated[str, Form()],
    workspace_id: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
):
    actor = request.app.state.web.require_owner(request)
    request.app.state.web.require_csrf(request, csrf)
    _asana_enabled(request)
    try:
        integration_id = request.app.state.asana.complete_site_authorization(
            pending_id, actor["id"], workspace_id
        )
    except (AsanaIntegrationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"], "asana.connected", "integration", integration_id
    )
    return RedirectResponse(
        f"/integrations/asana/{integration_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/jira/connect")
def jira_connect(request: Request):
    actor = request.app.state.web.require_owner(request)
    _jira_enabled(request)
    state_token = secrets.token_urlsafe(32)
    request.session["jira_oauth_pending"] = {
        "actor_id": actor["id"],
        "state": state_token,
        "issued_at": int(time.time()),
        "purpose": "site",
    }
    return RedirectResponse(
        request.app.state.jira.authorization_url(state_token),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/jira/callback")
async def jira_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
):
    # This endpoint completes both owner-level site authorization and
    # employee-level account authorization. The OAuth state binds the flow to the
    # initiating user; the site branch applies the stricter owner check below.
    actor = request.app.state.web.require_worker(request)
    _jira_enabled(request)
    pending = request.session.pop("jira_oauth_pending", None)
    now = int(time.time())
    if (
        not isinstance(pending, dict)
        or pending.get("actor_id") != actor["id"]
        or not isinstance(pending.get("state"), str)
        or not state
        or not hmac.compare_digest(pending["state"], state)
        or not isinstance(pending.get("issued_at"), int)
        or now - pending["issued_at"] not in range(0, 601)
    ):
        raise HTTPException(status_code=403, detail="Invalid or expired Jira state")
    if error:
        request.app.state.database.add_audit_event(
            actor["id"], "jira.connection_cancelled", "integration", None
        )
        raise HTTPException(status_code=422, detail="Jira authorization was cancelled")
    jira = request.app.state.jira
    purpose = pending.get("purpose")
    if purpose == "member":
        integration_id = pending.get("integration_id")
        integration = request.app.state.database.get_jira_integration(integration_id)
        if not integration or not integration["enabled"]:
            raise HTTPException(status_code=404, detail="Jira integration not found")
        try:
            values, expires_at = await asyncio.to_thread(jira.exchange_code, code)
            resources = await asyncio.to_thread(
                jira.accessible_resources, values["access_token"]
            )
            matching = [
                resource
                for resource in resources
                if resource["id"] == integration["provider_resource_key"]
            ]
            if len(resources) != 1 or len(matching) != 1:
                raise JiraIntegrationError(
                    "Authorize only the Jira site connected to this Dayfinch workspace",
                    error_code="site_mismatch",
                    retry_seconds=3600,
                )
            jira_identity = await asyncio.to_thread(
                jira.current_user, matching[0]["id"], values["access_token"]
            )
            jira.store_user_authorization(
                integration_id, actor["id"], values, expires_at
            )
            try:
                request.app.state.database.upsert_jira_user_connection(
                    integration_id,
                    actor["id"],
                    jira_identity["account_id"],
                    jira_identity["display_name"],
                    "member",
                )
            except ValueError:
                request.app.state.database.delete_user_integration_credentials(
                    integration_id, actor["id"], "jira"
                )
                raise
        except (JiraIntegrationError, ValueError) as exc:
            error_code = (
                exc.error_code
                if isinstance(exc, JiraIntegrationError)
                else "identity_conflict"
            )
            request.app.state.database.add_audit_event(
                actor["id"],
                "jira.user_connection_failed",
                "integration",
                integration_id,
                f"error_code={error_code}",
            )
            detail = (
                str(exc)
                if isinstance(exc, JiraIntegrationError)
                else "That Jira account is already connected to another Dayfinch user"
            )
            raise HTTPException(status_code=422, detail=detail) from exc
        request.app.state.database.add_audit_event(
            actor["id"], "jira.user_connected", "integration", integration_id
        )
        return RedirectResponse(
            f"/integrations/jira/{integration_id}/account",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if purpose != "site":
        raise HTTPException(status_code=403, detail="Invalid or expired Jira state")
    request.app.state.web.require_owner(request)
    try:
        values, expires_at = await asyncio.to_thread(jira.exchange_code, code)
        resources = await asyncio.to_thread(
            jira.accessible_resources, values["access_token"]
        )
        if len(resources) != 1:
            raise JiraIntegrationError(
                "Jira must grant exactly one resource-restricted site; update the "
                "Atlassian OAuth app and reconnect",
                error_code="ambiguous_resources",
                retry_seconds=3600,
            )
        resource = resources[0]
        jira_identity = await asyncio.to_thread(
            jira.current_user, resource["id"], values["access_token"]
        )
        database = request.app.state.database
        existing = database.get_jira_integration_by_resource(resource["id"])
        integration_id = database.upsert_jira_site(
            resource["id"], resource["name"], resource["url"], actor["id"]
        )
        duplicate_identity = next(
            (
                connection
                for connection in database.list_jira_user_connections(integration_id)
                if connection["atlassian_account_id"] == jira_identity["account_id"]
                and connection["user_id"] != actor["id"]
            ),
            None,
        )
        if duplicate_identity:
            if not existing:
                database.delete_unconfigured_jira_integration(integration_id)
            raise JiraIntegrationError(
                "That Jira account is already connected to another Dayfinch user",
                error_code="identity_conflict",
                retry_seconds=3600,
            )
        if database.jira_worklog_identity_conflicts(
            integration_id, actor["id"], jira_identity["account_id"]
        ):
            if not existing:
                database.delete_unconfigured_jira_integration(integration_id)
            raise JiraIntegrationError(
                "Reconnect the same Jira account that owns existing worklogs",
                error_code="identity_conflict",
                retry_seconds=3600,
            )
        try:
            jira.store_authorization(integration_id, values, expires_at)
            database.upsert_jira_user_connection(
                integration_id,
                actor["id"],
                jira_identity["account_id"],
                jira_identity["display_name"],
                "site",
            )
        except (JiraIntegrationError, ValueError) as exc:
            if not existing:
                database.delete_integration_credentials(integration_id, "jira")
                database.delete_unconfigured_jira_integration(integration_id)
            if isinstance(exc, JiraIntegrationError):
                raise
            raise JiraIntegrationError(
                "Jira user connection could not be stored",
                error_code="credential_storage_failed",
            ) from exc
    except JiraIntegrationError as exc:
        request.app.state.database.add_audit_event(
            actor["id"],
            "jira.connection_failed",
            "integration",
            None,
            f"error_code={exc.error_code}",
        )
        response_status = (
            503
            if exc.error_code
            in {
                "network_error",
                "provider_5xx",
                "rate_limited",
                "credential_storage_failed",
            }
            else 422
        )
        raise HTTPException(status_code=response_status, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"], "jira.connected", "integration", integration_id
    )
    return RedirectResponse(
        f"/integrations/jira/{integration_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _require_jira_account_scope(
    request: Request, integration_id: str
) -> tuple[dict, dict]:
    actor = request.app.state.web.require_worker(request)
    _jira_enabled(request)
    database = request.app.state.database
    integration = database.get_jira_integration(integration_id)
    if not integration or not integration["enabled"]:
        raise HTTPException(status_code=404, detail="Jira integration not found")
    if actor["role"] == "member" and not any(
        request.app.state.web.can_track_project(actor, mapping["project_id"])
        for mapping in database.jira_project_mappings(integration_id)
    ):
        raise HTTPException(status_code=403, detail="Jira project access required")
    return actor, integration


@router.get("/jira/{integration_id}/account", response_class=HTMLResponse)
def jira_account_page(request: Request, integration_id: str):
    actor, integration = _require_jira_account_scope(request, integration_id)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="jira_account.html",
        context=request.app.state.web.page_context(
            request,
            integration=integration,
            connection=request.app.state.database.jira_user_connection(
                integration_id, actor["id"]
            ),
        ),
    )


@router.get("/jira/{integration_id}/connect-account")
def connect_jira_account(request: Request, integration_id: str):
    actor, _integration = _require_jira_account_scope(request, integration_id)
    existing = request.app.state.database.jira_user_connection(
        integration_id, actor["id"]
    )
    if existing and existing["enabled"] and existing["credential_kind"] == "site":
        return RedirectResponse(
            f"/integrations/jira/{integration_id}/account",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    state_token = secrets.token_urlsafe(32)
    request.session["jira_oauth_pending"] = {
        "actor_id": actor["id"],
        "state": state_token,
        "issued_at": int(time.time()),
        "purpose": "member",
        "integration_id": integration_id,
    }
    return RedirectResponse(
        request.app.state.jira.authorization_url(state_token),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/jira/{integration_id}/account/settings")
def update_jira_account(
    request: Request,
    integration_id: str,
    time_sync_mode: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
):
    actor, _integration = _require_jira_account_scope(request, integration_id)
    request.app.state.web.require_csrf(request, csrf)
    try:
        request.app.state.database.set_jira_user_sync_mode(
            integration_id, actor["id"], time_sync_mode
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"],
        "jira.user_sync_mode_changed",
        "integration",
        integration_id,
        f"mode={time_sync_mode}",
    )
    return RedirectResponse(
        f"/integrations/jira/{integration_id}/account",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/jira/{integration_id}/disconnect-account")
def disconnect_jira_account(
    request: Request,
    integration_id: str,
    csrf: Annotated[str, Form()],
):
    actor, _integration = _require_jira_account_scope(request, integration_id)
    request.app.state.web.require_csrf(request, csrf)
    try:
        request.app.state.database.disconnect_jira_user(integration_id, actor["id"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"], "jira.user_disconnected", "integration", integration_id
    )
    return RedirectResponse(
        f"/integrations/jira/{integration_id}/account",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/jira/{integration_id}", response_class=HTMLResponse)
async def jira_integration_page(request: Request, integration_id: str):
    request.app.state.web.require_owner(request)
    _jira_enabled(request)
    database = request.app.state.database
    integration = database.get_jira_integration(integration_id)
    if not integration:
        raise HTTPException(status_code=404, detail="Jira integration not found")
    projects: list[dict] = []
    provider_error = ""
    if integration["enabled"]:
        try:
            projects = await asyncio.to_thread(
                request.app.state.jira.list_projects, integration_id
            )
        except JiraIntegrationError as exc:
            provider_error = str(exc)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="jira_integration.html",
        context=request.app.state.web.page_context(
            request,
            integration=integration,
            jira_projects=projects,
            mappings=database.jira_project_mappings(integration_id),
            projects=database.list_trackable_projects(),
            user_connections=database.list_jira_user_connections(integration_id),
            provider_error=provider_error,
        ),
    )


@router.post("/jira/{integration_id}/mappings")
async def jira_integration_mapping(
    request: Request,
    integration_id: str,
    external_project_id: Annotated[str, Form(min_length=1, max_length=255)],
    project_id: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
):
    actor = request.app.state.web.require_owner(request)
    request.app.state.web.require_csrf(request, csrf)
    _jira_enabled(request)
    database = request.app.state.database
    integration = database.get_jira_integration(integration_id)
    if not integration or not integration["enabled"]:
        raise HTTPException(status_code=404, detail="Jira integration not found")
    try:
        jira_projects = await asyncio.to_thread(
            request.app.state.jira.list_projects, integration_id
        )
    except JiraIntegrationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    external_project = next(
        (
            item
            for item in jira_projects
            if hmac.compare_digest(item["id"], external_project_id)
        ),
        None,
    )
    if not external_project:
        raise HTTPException(
            status_code=422, detail="Jira project access changed; reload"
        )
    try:
        database.set_jira_project_mapping(
            integration_id,
            external_project["id"],
            external_project["key"],
            external_project["name"],
            project_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    database.add_audit_event(
        actor["id"],
        "jira.project_mapped",
        "integration",
        integration_id,
        f"external_project_id={external_project['id']};project_id={project_id}",
    )
    return RedirectResponse(
        f"/integrations/jira/{integration_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/jira/{integration_id}/mappings/{external_project_id}/delete")
def delete_jira_integration_mapping(
    request: Request,
    integration_id: str,
    external_project_id: str,
    csrf: Annotated[str, Form()],
):
    actor = request.app.state.web.require_owner(request)
    request.app.state.web.require_csrf(request, csrf)
    try:
        request.app.state.database.remove_jira_project_mapping(
            integration_id, external_project_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"],
        "jira.project_unmapped",
        "integration",
        integration_id,
        f"external_project_id={external_project_id}",
    )
    return RedirectResponse(
        f"/integrations/jira/{integration_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/jira/{integration_id}/sync")
async def sync_jira_integration(
    request: Request,
    integration_id: str,
    csrf: Annotated[str, Form()],
):
    actor = request.app.state.web.require_owner(request)
    request.app.state.web.require_csrf(request, csrf)
    _jira_enabled(request)
    try:
        count = await asyncio.to_thread(request.app.state.jira.sync_now, integration_id)
    except JiraIntegrationError as exc:
        request.app.state.database.add_audit_event(
            actor["id"],
            "jira.sync_failed",
            "integration",
            integration_id,
            f"error_code={exc.error_code}",
        )
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"],
        "jira.synced",
        "integration",
        integration_id,
        f"issues={count}",
    )
    return RedirectResponse(
        f"/integrations/jira/{integration_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/jira/{integration_id}/disconnect")
def disconnect_jira_integration(
    request: Request,
    integration_id: str,
    csrf: Annotated[str, Form()],
):
    actor = request.app.state.web.require_owner(request)
    request.app.state.web.require_csrf(request, csrf)
    try:
        request.app.state.database.disconnect_jira_integration(integration_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"], "jira.disconnected", "integration", integration_id
    )
    return RedirectResponse("/settings", status_code=status.HTTP_303_SEE_OTHER)


def _require_asana_account_scope(
    request: Request, integration_id: str
) -> tuple[dict, dict]:
    actor = request.app.state.web.require_worker(request)
    _asana_enabled(request)
    database = request.app.state.database
    integration = database.get_asana_integration(integration_id)
    if not integration or not integration["enabled"]:
        raise HTTPException(status_code=404, detail="Asana integration not found")
    if actor["role"] == "member" and not any(
        request.app.state.web.can_track_project(actor, mapping["project_id"])
        for mapping in database.asana_project_mappings(integration_id)
    ):
        raise HTTPException(status_code=403, detail="Asana project access required")
    return actor, integration


@router.get("/asana/{integration_id}/account", response_class=HTMLResponse)
def asana_account_page(request: Request, integration_id: str):
    actor, integration = _require_asana_account_scope(request, integration_id)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="asana_account.html",
        context=request.app.state.web.page_context(
            request,
            integration=integration,
            connection=request.app.state.database.asana_user_connection(
                integration_id, actor["id"]
            ),
        ),
    )


@router.get("/asana/{integration_id}/connect-account")
def connect_asana_account(request: Request, integration_id: str):
    actor, _integration = _require_asana_account_scope(request, integration_id)
    existing = request.app.state.database.asana_user_connection(
        integration_id, actor["id"]
    )
    if existing and existing["enabled"] and existing["credential_kind"] == "site":
        return RedirectResponse(
            f"/integrations/asana/{integration_id}/account",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    state_token, verifier, challenge = oauth_state_values()
    request.session["asana_oauth_pending"] = {
        "actor_id": actor["id"],
        "state": state_token,
        "verifier": verifier,
        "issued_at": int(time.time()),
        "purpose": "member",
        "integration_id": integration_id,
    }
    return RedirectResponse(
        request.app.state.asana.authorization_url(state_token, challenge),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/asana/{integration_id}/account/settings")
def update_asana_account(
    request: Request,
    integration_id: str,
    time_sync_mode: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
):
    actor, _integration = _require_asana_account_scope(request, integration_id)
    request.app.state.web.require_csrf(request, csrf)
    try:
        request.app.state.database.set_asana_user_sync_mode(
            integration_id, actor["id"], time_sync_mode
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"],
        "asana.user_sync_mode_changed",
        "integration",
        integration_id,
        f"mode={time_sync_mode}",
    )
    return RedirectResponse(
        f"/integrations/asana/{integration_id}/account",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/asana/{integration_id}/disconnect-account")
async def disconnect_asana_account(
    request: Request,
    integration_id: str,
    csrf: Annotated[str, Form()],
):
    actor, _integration = _require_asana_account_scope(request, integration_id)
    request.app.state.web.require_csrf(request, csrf)
    connection = request.app.state.database.asana_user_connection(
        integration_id, actor["id"]
    )
    if not connection:
        raise HTTPException(status_code=404, detail="Asana connection not found")
    if connection["credential_kind"] == "member":
        await asyncio.to_thread(
            request.app.state.asana.revoke_stored,
            integration_id,
            subject_user_id=actor["id"],
        )
    try:
        request.app.state.database.disconnect_asana_user(integration_id, actor["id"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"], "asana.user_disconnected", "integration", integration_id
    )
    return RedirectResponse(
        f"/integrations/asana/{integration_id}/account",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/asana/{integration_id}", response_class=HTMLResponse)
async def asana_integration_page(request: Request, integration_id: str):
    request.app.state.web.require_owner(request)
    _asana_enabled(request)
    database = request.app.state.database
    integration = database.get_asana_integration(integration_id)
    if not integration:
        raise HTTPException(status_code=404, detail="Asana integration not found")
    projects: list[dict] = []
    provider_error = ""
    if integration["enabled"]:
        try:
            projects = await asyncio.to_thread(
                request.app.state.asana.list_projects, integration_id
            )
        except AsanaIntegrationError as exc:
            provider_error = str(exc)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="asana_integration.html",
        context=request.app.state.web.page_context(
            request,
            integration=integration,
            asana_projects=projects,
            mappings=database.asana_project_mappings(integration_id),
            projects=database.list_trackable_projects(),
            user_connections=database.list_asana_user_connections(integration_id),
            provider_error=provider_error,
        ),
    )


@router.post("/asana/{integration_id}/mappings")
async def asana_integration_mapping(
    request: Request,
    integration_id: str,
    external_project_id: Annotated[str, Form(min_length=1, max_length=200)],
    project_id: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
):
    actor = request.app.state.web.require_owner(request)
    request.app.state.web.require_csrf(request, csrf)
    _asana_enabled(request)
    database = request.app.state.database
    integration = database.get_asana_integration(integration_id)
    if not integration or not integration["enabled"]:
        raise HTTPException(status_code=404, detail="Asana integration not found")
    try:
        provider_projects = await asyncio.to_thread(
            request.app.state.asana.list_projects, integration_id
        )
    except AsanaIntegrationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    provider_project = next(
        (
            item
            for item in provider_projects
            if hmac.compare_digest(item["id"], external_project_id)
        ),
        None,
    )
    if not provider_project:
        raise HTTPException(
            status_code=422, detail="Asana project access changed; reload"
        )
    try:
        database.set_asana_project_mapping(
            integration_id,
            provider_project["id"],
            provider_project["name"],
            project_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    database.add_audit_event(
        actor["id"],
        "asana.project_mapped",
        "integration",
        integration_id,
        f"external_project_id={provider_project['id']};project_id={project_id}",
    )
    return RedirectResponse(
        f"/integrations/asana/{integration_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/asana/{integration_id}/mappings/{external_project_id}/delete")
def delete_asana_integration_mapping(
    request: Request,
    integration_id: str,
    external_project_id: str,
    csrf: Annotated[str, Form()],
):
    actor = request.app.state.web.require_owner(request)
    request.app.state.web.require_csrf(request, csrf)
    try:
        request.app.state.database.remove_asana_project_mapping(
            integration_id, external_project_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"],
        "asana.project_unmapped",
        "integration",
        integration_id,
        f"external_project_id={external_project_id}",
    )
    return RedirectResponse(
        f"/integrations/asana/{integration_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/asana/{integration_id}/sync")
async def sync_asana_integration(
    request: Request,
    integration_id: str,
    csrf: Annotated[str, Form()],
):
    actor = request.app.state.web.require_owner(request)
    request.app.state.web.require_csrf(request, csrf)
    _asana_enabled(request)
    try:
        count = await asyncio.to_thread(
            request.app.state.asana.sync_now, integration_id
        )
    except AsanaIntegrationError as exc:
        request.app.state.database.add_audit_event(
            actor["id"],
            "asana.sync_failed",
            "integration",
            integration_id,
            f"error_code={exc.error_code}",
        )
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"],
        "asana.synced",
        "integration",
        integration_id,
        f"tasks={count}",
    )
    return RedirectResponse(
        f"/integrations/asana/{integration_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/asana/{integration_id}/disconnect")
async def disconnect_asana_integration(
    request: Request,
    integration_id: str,
    csrf: Annotated[str, Form()],
):
    actor = request.app.state.web.require_owner(request)
    request.app.state.web.require_csrf(request, csrf)
    await asyncio.to_thread(request.app.state.asana.revoke_stored, integration_id)
    try:
        request.app.state.database.disconnect_asana_integration(integration_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"], "asana.disconnected", "integration", integration_id
    )
    return RedirectResponse("/settings", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/github/install")
def github_install(request: Request):
    actor = request.app.state.web.require_owner(request)
    _github_enabled(request)
    request.session["github_install_pending"] = {
        "actor_id": actor["id"],
        "issued_at": int(time.time()),
    }
    return RedirectResponse(
        request.app.state.github.installation_url(),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/github/setup")
def github_setup(
    request: Request,
    installation_id: Annotated[int, Query(gt=0)],
):
    actor = request.app.state.web.require_owner(request)
    _github_enabled(request)
    pending = request.session.pop("github_install_pending", None)
    now = int(time.time())
    if (
        not isinstance(pending, dict)
        or pending.get("actor_id") != actor["id"]
        or not isinstance(pending.get("issued_at"), int)
        or now - pending["issued_at"] not in range(0, 1801)
    ):
        raise HTTPException(
            status_code=403,
            detail="Start the GitHub installation from Dayfinch settings",
        )
    state_token, verifier, challenge = oauth_state_values()
    request.session["github_oauth_pending"] = {
        "actor_id": actor["id"],
        "installation_id": installation_id,
        "state": state_token,
        "verifier": verifier,
        "issued_at": now,
    }
    return RedirectResponse(
        request.app.state.github.authorization_url(state_token, challenge),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/github/callback")
async def github_callback(request: Request, code: str = "", state: str = ""):
    actor = request.app.state.web.require_owner(request)
    _github_enabled(request)
    pending = request.session.pop("github_oauth_pending", None)
    now = int(time.time())
    if (
        not isinstance(pending, dict)
        or pending.get("actor_id") != actor["id"]
        or not isinstance(pending.get("state"), str)
        or not state
        or not hmac.compare_digest(pending["state"], state)
        or not isinstance(pending.get("issued_at"), int)
        or now - pending["issued_at"] not in range(0, 601)
    ):
        raise HTTPException(status_code=403, detail="Invalid or expired GitHub state")
    github = request.app.state.github
    user_token = ""
    try:
        user_token = await asyncio.to_thread(
            github.exchange_user_code, code, pending["verifier"]
        )
        installation_id = int(pending["installation_id"])
        can_access = await asyncio.to_thread(
            github.user_can_access_installation, user_token, installation_id
        )
        if not can_access:
            raise GitHubIntegrationError(
                "The signed-in GitHub user cannot administer this installation",
                error_code="installation_ownership_failed",
            )
        installation = await asyncio.to_thread(github.installation, installation_id)
    except (GitHubIntegrationError, TypeError, ValueError) as exc:
        message = (
            str(exc)
            if isinstance(exc, GitHubIntegrationError)
            else "GitHub returned an invalid installation"
        )
        request.app.state.database.add_audit_event(
            actor["id"], "github.connection_failed", "integration", None, message
        )
        error_code = exc.error_code if isinstance(exc, GitHubIntegrationError) else ""
        response_status = (
            503
            if error_code in {"network_error", "provider_5xx", "rate_limited"}
            else 422
        )
        raise HTTPException(status_code=response_status, detail=message) from exc
    finally:
        if user_token:
            await asyncio.to_thread(github.revoke_user_token, user_token)
    try:
        integration_id = request.app.state.database.upsert_github_installation(
            installation["id"],
            installation["account_login"],
            installation["account_type"],
            actor["id"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"],
        "github.connected",
        "integration",
        integration_id,
        f"account_type={installation['account_type']}",
    )
    return RedirectResponse(
        f"/integrations/github/{integration_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/github/{integration_id}", response_class=HTMLResponse)
async def github_integration_page(request: Request, integration_id: str):
    request.app.state.web.require_owner(request)
    _github_enabled(request)
    database = request.app.state.database
    integration = database.get_github_integration(integration_id)
    if not integration:
        raise HTTPException(status_code=404, detail="GitHub integration not found")
    repositories: list[dict] = []
    provider_error = ""
    if integration["enabled"]:
        try:
            repositories = await asyncio.to_thread(
                request.app.state.github.list_repositories,
                int(integration["provider_external_id"]),
            )
        except GitHubIntegrationError as exc:
            provider_error = str(exc)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="github_integration.html",
        context=request.app.state.web.page_context(
            request,
            integration=integration,
            repositories=repositories,
            mappings=database.github_project_mappings(integration_id),
            projects=database.list_trackable_projects(),
            provider_error=provider_error,
        ),
    )


@router.post("/github/{integration_id}/mappings")
async def github_integration_mapping(
    request: Request,
    integration_id: str,
    repository: Annotated[str, Form(min_length=3, max_length=300)],
    project_id: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
):
    actor = request.app.state.web.require_owner(request)
    request.app.state.web.require_csrf(request, csrf)
    _github_enabled(request)
    database = request.app.state.database
    integration = database.get_github_integration(integration_id)
    if not integration or not integration["enabled"]:
        raise HTTPException(status_code=404, detail="GitHub integration not found")
    repository_id_text, separator, repository_name = repository.partition(":")
    try:
        repository_id = int(repository_id_text)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="Invalid GitHub repository"
        ) from exc
    if not separator or repository_id <= 0:
        raise HTTPException(status_code=422, detail="Invalid GitHub repository")
    try:
        repositories = await asyncio.to_thread(
            request.app.state.github.list_repositories,
            int(integration["provider_external_id"]),
        )
    except GitHubIntegrationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    repository = next(
        (
            item
            for item in repositories
            if int(item["id"]) == repository_id
            and hmac.compare_digest(str(item["full_name"]), repository_name)
        ),
        None,
    )
    if not repository:
        raise HTTPException(status_code=422, detail="Repository access changed; reload")
    try:
        database.set_github_project_mapping(
            integration_id, repository_id, repository_name, project_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    database.add_audit_event(
        actor["id"],
        "github.repository_mapped",
        "integration",
        integration_id,
        f"repository_id={repository_id};project_id={project_id}",
    )
    return RedirectResponse(
        f"/integrations/github/{integration_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/github/{integration_id}/mappings/{repository_id}/delete")
def delete_github_integration_mapping(
    request: Request,
    integration_id: str,
    repository_id: int,
    csrf: Annotated[str, Form()],
):
    actor = request.app.state.web.require_owner(request)
    request.app.state.web.require_csrf(request, csrf)
    try:
        request.app.state.database.remove_github_project_mapping(
            integration_id, repository_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"],
        "github.repository_unmapped",
        "integration",
        integration_id,
        f"repository_id={repository_id}",
    )
    return RedirectResponse(
        f"/integrations/github/{integration_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/github/{integration_id}/sync")
async def sync_github_integration(
    request: Request,
    integration_id: str,
    csrf: Annotated[str, Form()],
):
    actor = request.app.state.web.require_owner(request)
    request.app.state.web.require_csrf(request, csrf)
    _github_enabled(request)
    try:
        count = await asyncio.to_thread(
            request.app.state.github.sync_now, integration_id
        )
    except GitHubIntegrationError as exc:
        request.app.state.database.add_audit_event(
            actor["id"],
            "github.sync_failed",
            "integration",
            integration_id,
            f"error_code={exc.error_code}",
        )
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"],
        "github.synced",
        "integration",
        integration_id,
        f"issues={count}",
    )
    return RedirectResponse(
        f"/integrations/github/{integration_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/github/{integration_id}/disconnect")
def disconnect_github_integration(
    request: Request,
    integration_id: str,
    csrf: Annotated[str, Form()],
):
    actor = request.app.state.web.require_owner(request)
    request.app.state.web.require_csrf(request, csrf)
    try:
        request.app.state.database.disconnect_github_integration(integration_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"], "github.disconnected", "integration", integration_id
    )
    return RedirectResponse("/settings", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/github/webhook", include_in_schema=False)
async def github_webhook(request: Request) -> Response:
    _github_enabled(request)
    event_name = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", event_name) or not re.fullmatch(
        r"[A-Za-z0-9._-]{1,120}", delivery_id
    ):
        raise HTTPException(status_code=422, detail="Invalid GitHub webhook headers")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_GITHUB_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="GitHub webhook is too large")
    payload_bytes = bytes(body)
    if not request.app.state.github.verify_webhook(signature, payload_bytes):
        raise HTTPException(status_code=401, detail="Invalid GitHub webhook signature")
    try:
        payload = json.loads(payload_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=422, detail="Invalid GitHub webhook JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Invalid GitHub webhook payload")
    try:
        await asyncio.to_thread(
            request.app.state.github.handle_webhook,
            event_name,
            delivery_id,
            payload,
        )
    except GitHubIntegrationError as exc:
        raise HTTPException(
            status_code=503, detail="GitHub webhook processing failed"
        ) from exc
    return Response(status_code=status.HTTP_202_ACCEPTED)

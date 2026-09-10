from __future__ import annotations

import asyncio
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID

from fastapi import (
    APIRouter,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse

from ..schemas import FieldTimerEvent, LocationPoint
from ..services.invoice_vault import InvoiceVaultError
from ..services.payments import (
    MAX_PROVIDER_RESPONSE_BYTES,
    MAX_WISE_WEBHOOK_BYTES,
    PaymentDeliveryError,
)

router = APIRouter(tags=["workforce"])


def _page(request: Request, template: str, **context):
    web = request.app.state.web
    redirect = web.user_or_login(request)
    if redirect:
        return redirect
    return request.app.state.templates.TemplateResponse(
        request=request, name=template, context=web.page_context(request, **context)
    )


def _scope(user: dict) -> str | None:
    return None if user["role"] in {"admin", "manager"} else user["id"]


def _trackable_projects(database, user: dict) -> list[dict]:
    if user["role"] in {"admin", "manager"}:
        return database.list_trackable_projects()
    if user["role"] == "member":
        return database.list_trackable_projects(user["id"])
    return []


def _web_timer_allowed(database, user_id: str) -> bool:
    return database.effective_tracking_settings(user_id)["allowed_apps"] == "all"


def _require_web_timer_allowed(database, user_id: str) -> None:
    if not _web_timer_allowed(database, user_id):
        raise HTTPException(
            status_code=403, detail="Desktop tracking is required by policy"
        )


def _sso_form_values(request: Request, form) -> tuple[str, str]:
    sso_provider = str(form.get("sso_provider", ""))[:80]
    sso_domain = str(form.get("sso_domain", "")).strip().lower().lstrip("@")[:255]
    if sso_provider not in {"", "OpenID Connect", "SAML 2.0"}:
        raise HTTPException(status_code=422, detail="Unsupported SSO provider")
    if sso_provider == "OpenID Connect" and not request.app.state.oidc.enabled:
        raise HTTPException(
            status_code=422,
            detail="Configure the TRACKER_OIDC_* deployment secrets first",
        )
    if sso_provider == "SAML 2.0" and not request.app.state.saml.enabled:
        raise HTTPException(
            status_code=422,
            detail="Configure the TRACKER_SAML_* deployment secrets first",
        )
    if sso_provider and (
        not sso_domain
        or "." not in sso_domain
        or any(character in sso_domain for character in "@/: ")
    ):
        raise HTTPException(
            status_code=422,
            detail="Enter a valid SSO domain",
        )
    return sso_provider, sso_domain if sso_provider else ""


def _team_members(database, user: dict, permission: str) -> list[dict]:
    if user["role"] in {"admin", "manager"}:
        return database.list_users()
    return database.team_lead_members(user["id"], permission)


def _can_manage_team_user(
    database, user: dict, target_user_id: str, permission: str
) -> bool:
    return user["role"] in {"admin", "manager"} or database.team_lead_can_manage_user(
        user["id"], target_user_id, permission
    )


def _can_view_team_invoice(database, user: dict, invoice: dict) -> bool:
    return invoice["user_id"] == user["id"] or _can_manage_team_user(
        database, user, invoice["user_id"], "manage_financials"
    )


def _seal_team_invoice_or_defer(
    request: Request, user_id: str, invoice_id: str
) -> bool:
    try:
        request.app.state.invoice_vault.seal_team(invoice_id)
    except InvoiceVaultError:
        request.app.state.database.add_audit_event(
            user_id, "team_invoice.seal_deferred", "team_invoice", invoice_id
        )
        return False
    return True


@router.post("/timer/start")
def start_web_timer(
    request: Request,
    project_id: Annotated[str, Form()],
    task_id: Annotated[str, Form()] = "",
    csrf: Annotated[str, Form()] = "",
):
    web, db = request.app.state.web, request.app.state.database
    user = web.require_worker(request)
    web.require_csrf(request, csrf)
    _require_web_timer_allowed(db, user["id"])
    if not web.can_track_project(user, project_id):
        raise HTTPException(status_code=403, detail="Project is not assigned")
    device = db.web_timer_device(user["id"], project_id)
    try:
        db.sync_work_session(
            device, "active", task_id or None, project_id, transition=True
        )
    except ValueError as exc:
        code = 403 if str(exc) == "Desktop tracking is required by policy" else 422
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    db.add_audit_event(user["id"], "timer.started", "project", project_id, task_id)
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/timer/stop")
def stop_web_timer(request: Request, csrf: Annotated[str, Form()]):
    web, db = request.app.state.web, request.app.state.database
    user = web.require_worker(request)
    web.require_csrf(request, csrf)
    active = db.active_timer(user["id"])
    if active:
        device = db.get_device(active["device_id"])
        db.stop_break(user["id"])
        db.sync_work_session(
            device, "stopped", active.get("task_id"), active.get("project_id")
        )
        db.add_audit_event(user["id"], "timer.stopped", "work_session", active["id"])
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/timer/{action}")
def change_web_timer(request: Request, action: str, csrf: Annotated[str, Form()]):
    if action not in {"pause", "resume"}:
        raise HTTPException(status_code=404, detail="Unknown timer action")
    web, db = request.app.state.web, request.app.state.database
    user = web.require_worker(request)
    web.require_csrf(request, csrf)
    active = db.active_timer(user["id"])
    if active:
        if action == "resume":
            _require_web_timer_allowed(db, user["id"])
        device = db.get_device(active["device_id"])
        if action == "pause":
            db.start_break(user["id"], active["id"])
        else:
            db.stop_break(user["id"])
        try:
            db.sync_work_session(
                device,
                "paused" if action == "pause" else "active",
                active.get("task_id"),
                active.get("project_id"),
                transition=True,
            )
        except ValueError as exc:
            code = 403 if str(exc) == "Desktop tracking is required by policy" else 422
            raise HTTPException(status_code=code, detail=str(exc)) from exc
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/activity", response_class=HTMLResponse)
def activity_page(
    request: Request, user_id: str = "", project_id: str = "", tab: str = "screenshots"
):
    user = request.app.state.web.require_user(request)
    scope = _scope(user)
    chosen_user = user_id if scope is None and user_id else scope
    viewer_project_scope = user["id"] if user["role"] == "viewer" else None
    project_visibility_scope = user["id"] if user["role"] == "member" else None
    if viewer_project_scope:
        chosen_user = None
    if project_visibility_scope:
        chosen_user = None
    db = request.app.state.database
    return _page(
        request,
        "activity.html",
        tab=tab,
        records=db.activity_feed(
            chosen_user,
            project_id or None,
            viewer_project_scope,
            project_visibility_scope,
        ),
        apps=db.usage_summary(
            "active_app", chosen_user, viewer_project_scope, project_visibility_scope
        ),
        urls=db.usage_summary(
            "active_url", chosen_user, viewer_project_scope, project_visibility_scope
        ),
        users=db.list_users() if scope is None else [],
        projects=db.list_projects(scope),
        selected_user=user_id,
        selected_project=project_id,
    )


@router.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request):
    user = request.app.state.web.require_user(request)
    return _page(
        request,
        "notifications.html",
        notifications=request.app.state.database.list_notifications(user["id"]),
    )


@router.post("/notifications/read")
def read_notifications(request: Request, csrf: Annotated[str, Form()]):
    user = request.app.state.web.require_user(request)
    request.app.state.web.require_csrf(request, csrf)
    request.app.state.database.mark_notifications_read(user["id"])
    return RedirectResponse("/notifications", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/people", response_class=HTMLResponse)
def people_page(request: Request):
    actor = request.app.state.web.require_admin(request)
    provider = request.app.state.payroll_delivery.provider
    return _page(
        request,
        "people.html",
        users=request.app.state.database.list_users(),
        teams=request.app.state.database.list_teams(),
        projects=request.app.state.database.list_projects(),
        settings=request.app.state.database.organization_settings(),
        payment_provider=provider,
        payroll_destinations=(
            request.app.state.database.payroll_destinations(provider)
            if actor["role"] == "admin"
            else {}
        ),
    )


@router.post("/people/{user_id}/payroll-destination")
def update_payroll_destination(
    request: Request,
    user_id: str,
    provider: Annotated[str, Form()],
    recipient: Annotated[str, Form(max_length=127)],
    currency: Annotated[str, Form(max_length=3)] = "",
    confirmed: Annotated[bool, Form()] = False,
    csrf: Annotated[str, Form()] = "",
):
    owner = request.app.state.web.require_owner(request)
    request.app.state.web.require_csrf(request, csrf)
    recipient = recipient.strip().lower()
    if provider not in {"paypal", "wise"}:
        raise HTTPException(status_code=422, detail="Unsupported payroll provider")
    if not confirmed:
        raise HTTPException(
            status_code=422,
            detail=f"Confirm that this member owns the {provider.title()} destination",
        )
    if provider == "paypal" and not re.fullmatch(
        r"[^@\s]{1,64}@[^@\s]{1,62}", recipient
    ):
        raise HTTPException(status_code=422, detail="Enter a valid PayPal email")
    if provider == "wise" and (
        not recipient.isdigit()
        or int(recipient) <= 0
        or len(currency.strip()) != 3
        or not currency.strip().isalpha()
    ):
        raise HTTPException(
            status_code=422,
            detail="Enter a positive Wise recipient account ID and currency",
        )
    try:
        request.app.state.database.set_payroll_destination(
            user_id, provider, recipient, owner["id"], currency
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        owner["id"],
        "payroll.destination_confirmed",
        "user",
        user_id,
        provider,
    )
    return RedirectResponse("/people", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/people/{user_id}")
def update_person(
    request: Request,
    user_id: str,
    role: Annotated[str, Form()],
    full_name: Annotated[str, Form(max_length=120)],
    pay_rate: Annotated[str, Form()],
    bill_rate: Annotated[str, Form()],
    weekly_limit_minutes: Annotated[int, Form(ge=0, le=10080)],
    daily_limit_minutes: Annotated[int, Form(ge=0, le=1440)] = 480,
    manage_it: Annotated[bool, Form()] = False,
    csrf: Annotated[str, Form()] = "",
):
    actor = request.app.state.web.require_admin(request)
    request.app.state.web.require_csrf(request, csrf)
    target = request.app.state.database.get_user_any(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if actor["role"] == "manager":
        if target["role"] == "admin" or role == "admin":
            raise HTTPException(
                status_code=403, detail="Only the owner can assign owner access"
            )
        manage_it = bool(target.get("manage_it"))
    elif role == "admin" and target["role"] != "admin":
        raise HTTPException(
            status_code=403,
            detail="Ownership transfer requires a dedicated audited workflow",
        )
    elif target["id"] == actor["id"] and role != "admin":
        raise HTTPException(
            status_code=409, detail="The workspace owner cannot demote themselves"
        )
    try:
        request.app.state.database.set_user_profile(
            user_id,
            role,
            full_name,
            Decimal(pay_rate),
            Decimal(bill_rate),
            weekly_limit_minutes,
            daily_limit_minutes,
            manage_it,
        )
    except (ValueError, InvalidOperation) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"], "person.updated", "user", user_id, role
    )
    return RedirectResponse("/people", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/people/{user_id}/status")
def update_person_status(
    request: Request,
    user_id: str,
    enabled: Annotated[bool, Form()],
    csrf: Annotated[str, Form()],
):
    actor = request.app.state.web.require_admin(request)
    request.app.state.web.require_csrf(request, csrf)
    target = request.app.state.database.get_user_any(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target["role"] == "admin" and actor["role"] != "admin":
        raise HTTPException(
            status_code=403, detail="Only the owner can change owner status"
        )
    if actor["id"] == user_id and not enabled:
        raise HTTPException(
            status_code=422, detail="You cannot disable your own account"
        )
    request.app.state.database.set_user_enabled(user_id, enabled)
    request.app.state.database.add_audit_event(
        actor["id"], "person.status_changed", "user", user_id, str(enabled)
    )
    return RedirectResponse("/people", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/teams")
def add_team(
    request: Request,
    name: Annotated[str, Form(min_length=1, max_length=120)],
    lead_user_id: Annotated[str, Form()] = "",
    csrf: Annotated[str, Form()] = "",
):
    admin = request.app.state.web.require_admin(request)
    request.app.state.web.require_csrf(request, csrf)
    database = request.app.state.database
    if lead_user_id:
        lead = database.get_user(lead_user_id)
        if not lead or lead["role"] != "member" or not lead["enabled"]:
            raise HTTPException(
                status_code=422, detail="Choose an active member as team lead"
            )
    try:
        team_id = database.create_team(name, lead_user_id or None)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        admin["id"], "team.created", "team", team_id, name
    )
    return RedirectResponse("/people", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/teams/{team_id}/members")
def add_member_to_team(
    request: Request,
    team_id: str,
    user_id: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
):
    request.app.state.web.require_admin(request)
    request.app.state.web.require_csrf(request, csrf)
    database = request.app.state.database
    team = database.get_team(team_id)
    if not team or not database.get_user(user_id):
        raise HTTPException(status_code=404, detail="Team or member not found")
    if team.get("scim_managed"):
        raise HTTPException(status_code=409, detail="SCIM manages this team's members")
    database.add_team_member(team_id, user_id)
    return RedirectResponse("/people", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/teams/{team_id}/leads")
async def set_team_lead(request: Request, team_id: str):
    admin = request.app.state.web.require_admin(request)
    form = await request.form()
    request.app.state.web.require_csrf(request, str(form.get("csrf", "")))
    user_id = str(form.get("user_id", ""))
    database = request.app.state.database
    lead = database.get_user(user_id)
    team = database.get_team(team_id)
    if not team or not lead:
        raise HTTPException(status_code=404, detail="Team or member not found")
    if team.get("scim_managed"):
        raise HTTPException(status_code=409, detail="SCIM manages this team's members")
    if lead["role"] != "member" or not lead["enabled"]:
        raise HTTPException(
            status_code=422, detail="Choose an active member as team lead"
        )
    permissions = {name: name in form for name in database.TEAM_LEAD_PERMISSIONS}
    database.set_team_lead_permissions(team_id, user_id, permissions)
    database.add_audit_event(admin["id"], "team.lead_updated", "team", team_id, user_id)
    return RedirectResponse("/people", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/teams/{team_id}/projects")
def add_project_to_team(
    request: Request,
    team_id: str,
    project_id: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
):
    admin = request.app.state.web.require_admin(request)
    request.app.state.web.require_csrf(request, csrf)
    database = request.app.state.database
    if not database.get_team(team_id) or not database.get_project(project_id):
        raise HTTPException(status_code=404, detail="Team or project not found")
    database.add_team_project(team_id, project_id)
    database.add_audit_event(
        admin["id"], "team.project_added", "team", team_id, project_id
    )
    return RedirectResponse("/people", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/teams/{team_id}/projects/{project_id}/remove")
def remove_project_from_team(
    request: Request, team_id: str, project_id: str, csrf: Annotated[str, Form()]
):
    admin = request.app.state.web.require_admin(request)
    request.app.state.web.require_csrf(request, csrf)
    database = request.app.state.database
    if not database.get_team(team_id) or not database.get_project(project_id):
        raise HTTPException(status_code=404, detail="Team or project not found")
    database.remove_team_project(team_id, project_id)
    database.add_audit_event(
        admin["id"], "team.project_removed", "team", team_id, project_id
    )
    return RedirectResponse("/people", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/teams/{team_id}/members/{user_id}/remove")
def remove_member_from_team(
    request: Request,
    team_id: str,
    user_id: str,
    csrf: Annotated[str, Form()],
):
    request.app.state.web.require_admin(request)
    request.app.state.web.require_csrf(request, csrf)
    database = request.app.state.database
    team = database.get_team(team_id)
    if not team or not database.get_user(user_id):
        raise HTTPException(status_code=404, detail="Team or member not found")
    if team.get("scim_managed"):
        raise HTTPException(status_code=409, detail="SCIM manages this team's members")
    database.remove_team_member(team_id, user_id)
    return RedirectResponse("/people", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/time-entries", response_class=HTMLResponse)
def time_entries_page(request: Request):
    user = request.app.state.web.require_user(request)
    scope = _scope(user)
    db = request.app.state.database
    managed = _team_members(db, user, "approve_manual_time")
    can_review = user["role"] in {"admin", "manager"} or bool(managed)
    managed_ids = [user["id"], *(member["id"] for member in managed)]
    return _page(
        request,
        "time_entries.html",
        entries=db.list_manual_time(
            scope if not can_review else None,
            user_ids=managed_ids if can_review and scope is not None else None,
        ),
        projects=_trackable_projects(db, user),
        users=db.list_users() if scope is None else [],
        can_review_time=can_review,
    )


@router.post("/time-entries")
def add_time_entry(
    request: Request,
    project_id: Annotated[str, Form()],
    task_id: Annotated[str, Form()] = "",
    user_id: Annotated[str, Form()] = "",
    started_at: Annotated[str, Form()] = "",
    ended_at: Annotated[str, Form()] = "",
    note: Annotated[str, Form(max_length=500)] = "",
    csrf: Annotated[str, Form()] = "",
):
    web, db = request.app.state.web, request.app.state.database
    user = web.require_worker(request)
    web.require_csrf(request, csrf)
    policy = db.organization_settings()
    if user["role"] not in {"admin", "manager"} and not policy["allow_manual_time"]:
        raise HTTPException(status_code=403, detail="Manual time is disabled")
    if not web.can_track_project(user, project_id):
        raise HTTPException(status_code=403, detail="Project is not assigned")
    if task_id:
        task = db.get_task(task_id)
        if not task or task["project_id"] != project_id:
            raise HTTPException(
                status_code=422, detail="Task does not belong to project"
            )
    owner = user_id if user["role"] in {"admin", "manager"} and user_id else user["id"]
    target = db.get_user(owner)
    if not target:
        raise HTTPException(status_code=404, detail="Time-entry owner not found")
    if owner != user["id"]:
        if user["role"] == "manager" and target["role"] in {"admin", "manager"}:
            raise HTTPException(
                status_code=403, detail="Managers cannot add time for privileged users"
            )
        if target["role"] == "viewer" or (
            target["role"] == "member"
            and db.project_member_role(project_id, owner) not in {"worker", "manager"}
        ):
            raise HTTPException(
                status_code=403,
                detail="The selected user cannot track time for this project",
            )
    try:
        start, end = (
            datetime.fromisoformat(started_at),
            datetime.fromisoformat(ended_at),
        )
        db.add_manual_time(
            owner,
            project_id,
            task_id or None,
            start,
            end,
            note,
            auto_approve=user["role"] in {"admin", "manager"}
            or not policy["require_time_approval"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse("/time-entries", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/time-entries/{entry_id}/review")
def review_time_entry(
    request: Request,
    entry_id: str,
    decision: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
):
    web, database = request.app.state.web, request.app.state.database
    reviewer = web.require_user(request)
    web.require_csrf(request, csrf)
    owner_id = database.review_item_owner("manual_time_entries", entry_id)
    if not owner_id or not _can_manage_team_user(
        database, reviewer, owner_id, "approve_manual_time"
    ):
        raise HTTPException(status_code=404, detail="Manual-time request not found")
    database.review_item("manual_time_entries", entry_id, reviewer["id"], decision)
    return RedirectResponse("/time-entries", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/time-entries/bulk-review")
async def bulk_review_time_entries(request: Request):
    reviewer = request.app.state.web.require_user(request)
    database = request.app.state.database
    form = await request.form()
    request.app.state.web.require_csrf(request, str(form.get("csrf", "")))
    decision = str(form.get("decision", ""))
    item_ids = [str(value) for value in form.getlist("item_id")]
    if not item_ids:
        raise HTTPException(status_code=422, detail="Select at least one request")
    selected = list(dict.fromkeys(item_ids[:200]))
    for item_id in selected:
        owner_id = database.review_item_owner("manual_time_entries", item_id)
        if not owner_id or not _can_manage_team_user(
            database, reviewer, owner_id, "approve_manual_time"
        ):
            raise HTTPException(status_code=404, detail="Manual-time request not found")
    for item_id in selected:
        database.review_item("manual_time_entries", item_id, reviewer["id"], decision)
    database.add_audit_event(
        reviewer["id"],
        "manual_time.bulk_reviewed",
        "manual_time_entry",
        None,
        f"{decision}:{len(selected)}",
    )
    return RedirectResponse("/time-entries", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/schedules", response_class=HTMLResponse)
def schedules_page(request: Request):
    user = request.app.state.web.require_user(request)
    scope = _scope(user)
    db = request.app.state.database
    schedule_members = _team_members(db, user, "manage_schedules")
    time_off_members = _team_members(db, user, "approve_time_off")
    can_schedule = user["role"] in {"admin", "manager"} or bool(schedule_members)
    can_review_time_off = user["role"] in {"admin", "manager"} or bool(time_off_members)
    return _page(
        request,
        "schedules.html",
        shifts=db.list_shifts(
            scope if not can_schedule else None,
            user_ids=[user["id"], *(member["id"] for member in schedule_members)]
            if can_schedule and scope is not None
            else None,
        ),
        time_off=db.list_time_off(
            scope if not can_review_time_off else None,
            user_ids=[user["id"], *(member["id"] for member in time_off_members)]
            if can_review_time_off and scope is not None
            else None,
        ),
        users=db.list_users() if scope is None else schedule_members,
        projects=db.list_projects(scope),
        holidays=db.list_holidays(),
        can_schedule=can_schedule,
        can_review_time_off=can_review_time_off,
    )


@router.post("/holidays")
def add_holiday(
    request: Request,
    name: Annotated[str, Form(min_length=1, max_length=120)],
    holiday_date: Annotated[str, Form()],
    paid_minutes: Annotated[int, Form(ge=0, le=1440)] = 480,
    csrf: Annotated[str, Form()] = "",
):
    admin = request.app.state.web.require_admin(request)
    request.app.state.web.require_csrf(request, csrf)
    holiday_id = request.app.state.database.create_holiday(
        name, date.fromisoformat(holiday_date), paid_minutes
    )
    request.app.state.database.add_audit_event(
        admin["id"], "holiday.created", "holiday", holiday_id, name
    )
    return RedirectResponse("/schedules", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/locations", response_class=HTMLResponse)
def locations_page(request: Request):
    user = request.app.state.web.require_user(request)
    scope = _scope(user)
    db = request.app.state.database
    return _page(
        request,
        "locations.html",
        locations=db.list_locations(scope),
        geofences=db.list_geofences(scope),
        projects=db.list_projects(scope),
        attendance=db.attendance_report(scope),
    )


@router.get("/field", response_class=HTMLResponse)
def field_tracker_page(request: Request):
    user = request.app.state.web.require_worker(request)
    db = request.app.state.database
    return _page(
        request,
        "field_tracker.html",
        projects=_trackable_projects(db, user),
        active_timer=db.active_timer(user["id"]),
        web_timer_allowed=_web_timer_allowed(db, user["id"]),
    )


@router.post("/field/location", status_code=201)
def field_location(
    request: Request,
    payload: LocationPoint,
    x_csrf_token: Annotated[str, Header()],
):
    web, db = request.app.state.web, request.app.state.database
    user = web.require_worker(request)
    web.require_csrf(request, x_csrf_token)
    recorded = payload.recorded_at
    if recorded.tzinfo is None:
        raise HTTPException(status_code=422, detail="recorded_at must include timezone")
    now = datetime.now(recorded.tzinfo)
    if recorded > now + timedelta(minutes=5) or recorded < now - timedelta(days=90):
        raise HTTPException(
            status_code=422, detail="location timestamp is outside replay window"
        )
    current, previous = db.geofence_state(
        user["id"], payload.latitude, payload.longitude
    )
    event_type = "position"
    event_fence = current
    if current and (not previous or current["id"] != previous["id"]):
        event_type = "enter"
    elif not current and previous:
        event_type = "exit"
        event_fence = previous
    active = db.active_timer(user["id"])
    project_id = (
        (active or {}).get("project_id")
        or (event_fence or {}).get("project_id")
        or next((p["id"] for p in _trackable_projects(db, user)), None)
    )
    device = db.web_timer_device(user["id"], project_id)
    created = db.add_location(
        {
            "id": str(payload.event_id),
            "device_id": device["id"],
            "user_id": user["id"],
            "session_id": active["id"] if active else None,
            "recorded_at": payload.recorded_at,
            "latitude": payload.latitude,
            "longitude": payload.longitude,
            "accuracy_meters": payload.accuracy_meters,
            "geofence_id": (event_fence or {}).get("id"),
            "event_type": event_type,
        }
    )
    if (
        created
        and event_fence
        and event_type == "enter"
        and event_fence["enter_action"] == "start"
        and not active
    ):
        if (
            _web_timer_allowed(db, user["id"])
            and event_fence.get("project_id")
            and web.can_track_project(user, event_fence["project_id"])
        ):
            try:
                db.sync_work_session(
                    device,
                    "active",
                    None,
                    event_fence["project_id"],
                    transition=True,
                )
            except ValueError:
                # The location event remains valid if a project or app policy
                # changes concurrently; only the optional timer action is skipped.
                pass
    elif (
        created
        and event_fence
        and event_type == "exit"
        and event_fence["exit_action"] == "stop"
        and active
    ):
        db.sync_work_session(
            device, "stopped", active.get("task_id"), active.get("project_id")
        )
    return {
        "status": "created" if created else "duplicate",
        "event_type": event_type,
        "geofence": (event_fence or {}).get("name", ""),
    }


@router.post("/field/timer", status_code=201)
def field_timer_event(
    request: Request,
    payload: FieldTimerEvent,
    x_csrf_token: Annotated[str, Header()],
):
    web, db = request.app.state.web, request.app.state.database
    user = web.require_worker(request)
    web.require_csrf(request, x_csrf_token)
    observed = payload.observed_at
    if observed.tzinfo is None:
        raise HTTPException(status_code=422, detail="observed_at must include timezone")
    now = datetime.now(observed.tzinfo)
    if observed > now + timedelta(minutes=5) or observed < now - timedelta(days=90):
        raise HTTPException(
            status_code=422, detail="timer timestamp is outside replay window"
        )
    project_id = str(payload.project_id) if payload.project_id else None
    task_id = str(payload.task_id) if payload.task_id else None
    active = db.active_timer(user["id"])
    if payload.action in {"start", "resume"}:
        _require_web_timer_allowed(db, user["id"])
    if payload.action == "start":
        if not project_id or not web.can_track_project(user, project_id):
            raise HTTPException(status_code=403, detail="Project is not assigned")
    elif active:
        project_id = active.get("project_id")
        task_id = active.get("task_id")
    device = db.web_timer_device(user["id"], project_id)
    state = {
        "start": "active",
        "pause": "paused",
        "resume": "active",
        "stop": "stopped",
    }[payload.action]
    try:
        session = db.sync_work_session(
            device,
            state,
            task_id,
            project_id,
            event_id=str(payload.event_id),
            observed_at=observed,
            heartbeat_interval_seconds=60,
            transition=True,
        )
    except ValueError as exc:
        code = 403 if str(exc) == "Desktop tracking is required by policy" else 422
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    return {"status": "accepted", "session_id": (session or {}).get("id")}


@router.post("/geofences")
def add_geofence(
    request: Request,
    name: Annotated[str, Form(min_length=1, max_length=120)],
    project_id: Annotated[str, Form()] = "",
    latitude: Annotated[float, Form(ge=-90, le=90)] = 0,
    longitude: Annotated[float, Form(ge=-180, le=180)] = 0,
    radius_meters: Annotated[int, Form(ge=25, le=100000)] = 200,
    enter_action: Annotated[str, Form()] = "none",
    exit_action: Annotated[str, Form()] = "none",
    csrf: Annotated[str, Form()] = "",
):
    admin = request.app.state.web.require_admin(request)
    request.app.state.web.require_csrf(request, csrf)
    if project_id:
        project = request.app.state.database.get_project(project_id)
        if not project or not project["enabled"]:
            raise HTTPException(status_code=404, detail="Project not found")
    geofence_id = request.app.state.database.create_geofence(
        name,
        project_id or None,
        latitude,
        longitude,
        radius_meters,
        enter_action,
        exit_action,
    )
    request.app.state.database.add_audit_event(
        admin["id"], "geofence.created", "geofence", geofence_id, name
    )
    return RedirectResponse("/locations", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/schedules")
def add_schedule(
    request: Request,
    user_id: Annotated[str, Form()],
    project_id: Annotated[str, Form()] = "",
    starts_at: Annotated[str, Form()] = "",
    ends_at: Annotated[str, Form()] = "",
    notes: Annotated[str, Form(max_length=500)] = "",
    csrf: Annotated[str, Form()] = "",
):
    web, database = request.app.state.web, request.app.state.database
    scheduler = web.require_user(request)
    web.require_csrf(request, csrf)
    if not _can_manage_team_user(database, scheduler, user_id, "manage_schedules"):
        raise HTTPException(status_code=404, detail="Team member not found")
    target = database.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="Team member not found")
    if target["role"] == "viewer":
        raise HTTPException(
            status_code=422, detail="Project viewers cannot be scheduled"
        )
    if scheduler["role"] == "manager" and target["role"] == "admin":
        raise HTTPException(
            status_code=403, detail="Managers cannot schedule the workspace owner"
        )
    if project_id:
        project = database.get_project(project_id)
        if not project or not project["enabled"]:
            raise HTTPException(status_code=404, detail="Project not found")
        if target["role"] == "member" and database.project_member_role(
            project_id, user_id
        ) not in {"worker", "manager"}:
            raise HTTPException(
                status_code=422, detail="Team member is not assigned to that project"
            )
        if scheduler["role"] not in {
            "admin",
            "manager",
        } and not database.team_lead_can_schedule_user_project(
            scheduler["id"], user_id, project_id
        ):
            raise HTTPException(status_code=404, detail="Team project not found")
    try:
        database.add_shift(
            user_id,
            project_id or None,
            datetime.fromisoformat(starts_at),
            datetime.fromisoformat(ends_at),
            notes,
            scheduler["id"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse("/schedules", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/time-off")
def request_time_off(
    request: Request,
    category: Annotated[str, Form()],
    starts_on: Annotated[str, Form()],
    ends_on: Annotated[str, Form()],
    minutes: Annotated[int, Form(ge=0)],
    reason: Annotated[str, Form(max_length=500)],
    csrf: Annotated[str, Form()],
):
    user = request.app.state.web.require_worker(request)
    request.app.state.web.require_csrf(request, csrf)
    try:
        request.app.state.database.add_time_off(
            user["id"],
            category,
            date.fromisoformat(starts_on),
            date.fromisoformat(ends_on),
            minutes,
            reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse("/schedules", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/time-off/{item_id}/review")
def review_time_off(
    request: Request,
    item_id: str,
    decision: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
):
    web, database = request.app.state.web, request.app.state.database
    reviewer = web.require_user(request)
    web.require_csrf(request, csrf)
    owner_id = database.review_item_owner("time_off_requests", item_id)
    if not owner_id or not _can_manage_team_user(
        database, reviewer, owner_id, "approve_time_off"
    ):
        raise HTTPException(status_code=404, detail="Time-off request not found")
    database.review_item("time_off_requests", item_id, reviewer["id"], decision)
    return RedirectResponse("/schedules", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/financials", response_class=HTMLResponse)
def financials_page(request: Request):
    user = request.app.state.web.require_user(request)
    scope = _scope(user)
    db = request.app.state.database
    managed = _team_members(db, user, "manage_financials")
    can_review_expenses = user["role"] in {"admin", "manager"} or bool(managed)
    return _page(
        request,
        "financials.html",
        finance=db.finance_summary(include_payroll=user["role"] == "admin")
        if scope is None
        else {},
        expenses=db.list_expenses(
            scope if not can_review_expenses else None,
            user_ids=[user["id"], *(member["id"] for member in managed)]
            if can_review_expenses and scope is not None
            else None,
        ),
        projects=_trackable_projects(db, user),
        users=db.list_users() if scope is None else [],
        payroll_provider_configured=request.app.state.payroll_delivery.configured,
        payroll_provider=request.app.state.payroll_delivery.provider,
        can_review_expenses=can_review_expenses,
        team_invoices=db.list_team_invoices(
            user["id"] if scope is not None and not can_review_expenses else None,
            user_ids=[user["id"], *(member["id"] for member in managed)]
            if can_review_expenses and scope is not None
            else None,
        ),
        can_manage_team_invoices=can_review_expenses,
        today=datetime.now(UTC).date(),
    )


@router.post("/expenses")
def add_expense(
    request: Request,
    project_id: Annotated[str, Form()] = "",
    incurred_on: Annotated[str, Form()] = "",
    category: Annotated[str, Form()] = "other",
    amount: Annotated[str, Form()] = "0",
    currency: Annotated[str, Form()] = "USD",
    description: Annotated[str, Form(max_length=500)] = "",
    csrf: Annotated[str, Form()] = "",
):
    user = request.app.state.web.require_worker(request)
    request.app.state.web.require_csrf(request, csrf)
    if project_id and not request.app.state.web.can_track_project(user, project_id):
        raise HTTPException(status_code=403, detail="Project is not assigned")
    try:
        request.app.state.database.add_expense(
            user["id"],
            project_id or None,
            date.fromisoformat(incurred_on),
            category,
            Decimal(amount),
            currency,
            description,
        )
    except (ValueError, InvalidOperation) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse("/financials", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/expenses/{item_id}/review")
def review_expense(
    request: Request,
    item_id: str,
    decision: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
):
    web, database = request.app.state.web, request.app.state.database
    reviewer = web.require_user(request)
    web.require_csrf(request, csrf)
    owner_id = database.review_item_owner("expenses", item_id)
    if not owner_id or not _can_manage_team_user(
        database, reviewer, owner_id, "manage_financials"
    ):
        raise HTTPException(status_code=404, detail="Expense not found")
    database.review_item("expenses", item_id, reviewer["id"], decision)
    return RedirectResponse("/financials", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/clients")
def add_client(
    request: Request,
    name: Annotated[str, Form(min_length=1, max_length=120)],
    email: Annotated[str, Form(max_length=254)] = "",
    address: Annotated[str, Form(max_length=500)] = "",
    csrf: Annotated[str, Form()] = "",
):
    admin = request.app.state.web.require_admin(request)
    request.app.state.web.require_csrf(request, csrf)
    client_id = request.app.state.database.create_client(name, email, address)
    request.app.state.database.add_audit_event(
        admin["id"], "client.created", "client", client_id, name
    )
    return RedirectResponse("/financials", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/invoices")
def add_invoice(
    request: Request,
    client_id: Annotated[str, Form()],
    issued_on: Annotated[str, Form()],
    due_on: Annotated[str, Form()],
    description: Annotated[str, Form(max_length=500)],
    quantity: Annotated[str, Form()],
    unit_price: Annotated[str, Form()],
    currency: Annotated[str, Form()] = "USD",
    csrf: Annotated[str, Form()] = "",
):
    admin = request.app.state.web.require_admin(request)
    request.app.state.web.require_csrf(request, csrf)
    try:
        invoice_id = request.app.state.database.create_invoice(
            client_id,
            date.fromisoformat(issued_on),
            date.fromisoformat(due_on),
            description,
            Decimal(quantity),
            Decimal(unit_price),
            currency,
            admin["id"],
        )
        request.app.state.invoice_vault.seal(invoice_id)
    except (ValueError, InvalidOperation, InvoiceVaultError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse("/financials", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/invoices/{invoice_id}/document", response_class=HTMLResponse)
def invoice_document(request: Request, invoice_id: str):
    request.app.state.web.require_admin(request)
    try:
        invoice = request.app.state.invoice_vault.open(invoice_id)
    except InvoiceVaultError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="invoice_document.html",
        context={"request": request, "invoice": invoice},
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/invoices/{invoice_id}/seal")
def seal_invoice(
    request: Request,
    invoice_id: str,
    csrf: Annotated[str, Form()],
):
    admin = request.app.state.web.require_admin(request)
    request.app.state.web.require_csrf(request, csrf)
    try:
        request.app.state.invoice_vault.seal(invoice_id)
    except InvoiceVaultError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        admin["id"], "invoice.sealed", "invoice", invoice_id
    )
    return RedirectResponse("/financials", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/team-invoices/manual")
def add_manual_team_invoice(
    request: Request,
    issued_on: Annotated[str, Form()],
    due_on: Annotated[str, Form()],
    description: Annotated[str, Form(min_length=1, max_length=500)],
    quantity: Annotated[str, Form()],
    unit_price: Annotated[str, Form()],
    purchase_order: Annotated[str, Form(max_length=120)] = "",
    notes: Annotated[str, Form(max_length=1000)] = "",
    currency: Annotated[str, Form()] = "USD",
    csrf: Annotated[str, Form()] = "",
):
    web = request.app.state.web
    user = web.require_worker(request)
    web.require_csrf(request, csrf)
    try:
        invoice_id = request.app.state.database.create_team_invoice(
            user["id"],
            date.fromisoformat(issued_on),
            date.fromisoformat(due_on),
            description,
            Decimal(quantity),
            Decimal(unit_price),
            currency,
            purchase_order,
            notes,
        )
    except (ValueError, InvalidOperation) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _seal_team_invoice_or_defer(request, user["id"], invoice_id)
    request.app.state.database.add_audit_event(
        user["id"], "team_invoice.created", "team_invoice", invoice_id
    )
    return RedirectResponse("/financials", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/team-invoices/tracked")
def add_tracked_team_invoice(
    request: Request,
    issued_on: Annotated[str, Form()],
    due_on: Annotated[str, Form()],
    period_start: Annotated[str, Form()],
    period_end: Annotated[str, Form()],
    project_id: Annotated[str, Form()] = "",
    purchase_order: Annotated[str, Form(max_length=120)] = "",
    notes: Annotated[str, Form(max_length=1000)] = "",
    currency: Annotated[str, Form()] = "USD",
    csrf: Annotated[str, Form()] = "",
):
    web = request.app.state.web
    user = web.require_worker(request)
    web.require_csrf(request, csrf)
    if project_id and not web.can_track_project(user, project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        invoice_id = request.app.state.database.create_team_invoice_from_time(
            user["id"],
            date.fromisoformat(issued_on),
            date.fromisoformat(due_on),
            date.fromisoformat(period_start),
            date.fromisoformat(period_end),
            project_id or None,
            currency,
            purchase_order,
            notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _seal_team_invoice_or_defer(request, user["id"], invoice_id)
    request.app.state.database.add_audit_event(
        user["id"], "team_invoice.generated", "team_invoice", invoice_id
    )
    return RedirectResponse("/financials", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/team-invoices/{invoice_id}/submit")
def submit_team_invoice(
    request: Request, invoice_id: str, csrf: Annotated[str, Form()]
):
    web = request.app.state.web
    user = web.require_worker(request)
    web.require_csrf(request, csrf)
    try:
        request.app.state.database.submit_team_invoice(invoice_id, user["id"])
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _seal_team_invoice_or_defer(request, user["id"], invoice_id)
    request.app.state.database.add_audit_event(
        user["id"], "team_invoice.submitted", "team_invoice", invoice_id
    )
    return RedirectResponse("/financials", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/team-invoices/{invoice_id}")
def update_team_invoice(
    request: Request,
    invoice_id: str,
    issued_on: Annotated[str, Form()],
    due_on: Annotated[str, Form()],
    purchase_order: Annotated[str, Form(max_length=120)] = "",
    notes: Annotated[str, Form(max_length=1000)] = "",
    description: Annotated[str, Form(max_length=500)] = "",
    quantity: Annotated[str, Form()] = "",
    unit_price: Annotated[str, Form()] = "",
    csrf: Annotated[str, Form()] = "",
):
    web = request.app.state.web
    user = web.require_worker(request)
    web.require_csrf(request, csrf)
    database = request.app.state.database
    invoice = database.team_invoice_snapshot(invoice_id)
    if not invoice or invoice["user_id"] != user["id"] or invoice["status"] != "draft":
        raise HTTPException(status_code=404, detail="Draft team invoice not found")
    manual = bool(invoice["lines"] and invoice["lines"][0]["source"] == "manual")
    try:
        database.update_team_invoice_draft(
            invoice_id,
            user["id"],
            date.fromisoformat(issued_on),
            date.fromisoformat(due_on),
            purchase_order,
            notes,
            description if manual else None,
            Decimal(quantity) if manual else None,
            Decimal(unit_price) if manual else None,
        )
    except (ValueError, InvalidOperation) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _seal_team_invoice_or_defer(request, user["id"], invoice_id)
    database.add_audit_event(
        user["id"], "team_invoice.updated", "team_invoice", invoice_id
    )
    return RedirectResponse(
        f"/team-invoices/{invoice_id}/document",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/team-invoices/{invoice_id}/delete")
def delete_team_invoice(
    request: Request, invoice_id: str, csrf: Annotated[str, Form()]
):
    web = request.app.state.web
    user = web.require_worker(request)
    web.require_csrf(request, csrf)
    invoice = request.app.state.database.team_invoice_snapshot(invoice_id)
    try:
        request.app.state.database.delete_team_invoice_draft(invoice_id, user["id"])
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if invoice and invoice.get("encrypted_document_key"):
        request.app.state.storage.delete(
            invoice["encrypted_document_key"], invoice.get("document_version_id")
        )
    request.app.state.database.add_audit_event(
        user["id"], "team_invoice.deleted", "team_invoice", invoice_id
    )
    return RedirectResponse("/financials", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/team-invoices/{invoice_id}/document", response_class=HTMLResponse)
def team_invoice_document(request: Request, invoice_id: str):
    web = request.app.state.web
    user = web.require_user(request)
    database = request.app.state.database
    live = database.team_invoice_snapshot(invoice_id)
    if not live or not _can_view_team_invoice(database, user, live):
        raise HTTPException(status_code=404, detail="Team invoice not found")
    try:
        if not live.get("document_sealed_at") or (
            live.get("updated_at") and live["document_sealed_at"] < live["updated_at"]
        ):
            request.app.state.invoice_vault.seal_team(invoice_id)
        invoice = request.app.state.invoice_vault.open_team(invoice_id)
    except InvoiceVaultError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="team_invoice_document.html",
        context={
            "request": request,
            "invoice": invoice,
            "csrf_token": web.csrf_token(request),
            "can_edit": live["user_id"] == user["id"] and live["status"] == "draft",
            "can_void": live["status"] == "submitted"
            and _can_manage_team_user(
                database, user, live["user_id"], "manage_financials"
            ),
        },
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/team-invoices/{invoice_id}/payments")
def record_team_invoice_payment(
    request: Request,
    invoice_id: str,
    amount: Annotated[str, Form()],
    paid_on: Annotated[str, Form()],
    reference: Annotated[str, Form(max_length=200)] = "",
    csrf: Annotated[str, Form()] = "",
):
    web = request.app.state.web
    user = web.require_user(request)
    web.require_csrf(request, csrf)
    database = request.app.state.database
    invoice = database.team_invoice_snapshot(invoice_id)
    if not invoice or not _can_manage_team_user(
        database, user, invoice["user_id"], "manage_financials"
    ):
        raise HTTPException(status_code=404, detail="Team invoice not found")
    try:
        payment_id = database.record_team_invoice_payment(
            invoice_id,
            Decimal(amount),
            date.fromisoformat(paid_on),
            reference,
            user["id"],
        )
    except (ValueError, InvalidOperation) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _seal_team_invoice_or_defer(request, user["id"], invoice_id)
    database.add_audit_event(
        user["id"],
        "team_invoice.payment_recorded",
        "team_invoice",
        invoice_id,
        payment_id,
    )
    return RedirectResponse("/financials", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/team-invoices/{invoice_id}/void")
def void_team_invoice(request: Request, invoice_id: str, csrf: Annotated[str, Form()]):
    web = request.app.state.web
    user = web.require_user(request)
    web.require_csrf(request, csrf)
    database = request.app.state.database
    invoice = database.team_invoice_snapshot(invoice_id)
    if not invoice or not _can_manage_team_user(
        database, user, invoice["user_id"], "manage_financials"
    ):
        raise HTTPException(status_code=404, detail="Team invoice not found")
    try:
        database.void_team_invoice(invoice_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _seal_team_invoice_or_defer(request, user["id"], invoice_id)
    database.add_audit_event(
        user["id"], "team_invoice.voided", "team_invoice", invoice_id
    )
    return RedirectResponse("/financials", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/payroll")
def add_payroll(
    request: Request,
    user_id: Annotated[str, Form()],
    period_start: Annotated[str, Form()],
    period_end: Annotated[str, Form()],
    currency: Annotated[str, Form()] = "USD",
    csrf: Annotated[str, Form()] = "",
):
    admin = request.app.state.web.require_owner(request)
    request.app.state.web.require_csrf(request, csrf)
    try:
        request.app.state.database.create_payroll(
            user_id,
            date.fromisoformat(period_start),
            date.fromisoformat(period_end),
            currency,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        admin["id"], "payroll.created", "user", user_id, period_start
    )
    return RedirectResponse("/financials", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/financials/payroll/{payment_id}/send")
def send_payroll(
    request: Request,
    payment_id: str,
    csrf: Annotated[str, Form()],
):
    admin = request.app.state.web.require_owner(request)
    request.app.state.web.require_csrf(request, csrf)
    try:
        payment = request.app.state.payroll_delivery.send(payment_id)
    except PaymentDeliveryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        admin["id"],
        "payroll.dispatched",
        "payroll",
        payment_id,
        payment.get("external_reference", ""),
    )
    return RedirectResponse("/financials", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/financials/payroll/{payment_id}/reconcile")
def reconcile_payroll(
    request: Request,
    payment_id: str,
    csrf: Annotated[str, Form()],
):
    admin = request.app.state.web.require_owner(request)
    request.app.state.web.require_csrf(request, csrf)
    try:
        payment = request.app.state.payroll_delivery.reconcile(payment_id)
    except PaymentDeliveryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        admin["id"],
        "payroll.reconciled",
        "payroll",
        payment_id,
        payment.get("status", ""),
    )
    return RedirectResponse("/financials", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/api/v1/payroll/provider-callback", include_in_schema=False)
async def payroll_provider_callback(
    request: Request,
    x_dayfinch_signature: Annotated[str, Header()] = "",
):
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > MAX_PROVIDER_RESPONSE_BYTES:
            raise HTTPException(status_code=413, detail="Payroll callback is too large")
    try:
        payment = request.app.state.payroll_delivery.handle_callback(
            bytes(payload), x_dayfinch_signature
        )
    except PaymentDeliveryError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        None,
        "payroll.provider_callback",
        "payroll",
        payment["id"],
        payment["status"],
    )
    return {"status": "accepted"}


@router.post("/api/v1/payroll/wise-webhook", include_in_schema=False)
async def wise_payroll_webhook(request: Request) -> Response:
    service = request.app.state.payroll_delivery
    if not service.wise_webhook_enabled:
        raise HTTPException(status_code=404, detail="Wise webhook is unavailable")
    signature = request.headers.get("X-Signature-SHA256", "")
    delivery_id = request.headers.get("X-Delivery-Id", "")
    test_header = request.headers.get("X-Test-Notification", "")
    if (
        not re.fullmatch(r"[A-Fa-f0-9-]{36}", delivery_id)
        or not signature
        or len(signature) > 1000
        or test_header.lower() not in {"", "false", "true"}
    ):
        raise HTTPException(status_code=422, detail="Invalid Wise webhook headers")
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > MAX_WISE_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="Wise webhook is too large")
    try:
        await asyncio.to_thread(
            service.handle_wise_webhook,
            bytes(payload),
            signature,
            delivery_id,
            test_notification=test_header.lower() == "true",
        )
    except PaymentDeliveryError as exc:
        reason = str(exc)
        if reason == "wise_webhook_invalid_signature":
            raise HTTPException(
                status_code=401, detail="Invalid Wise webhook signature"
            ) from exc
        if "invalid" in reason or "unsupported" in reason:
            raise HTTPException(
                status_code=422, detail="Invalid Wise webhook payload"
            ) from exc
        raise HTTPException(
            status_code=503, detail="Wise webhook processing failed"
        ) from exc
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/financials/{kind}/{item_id}/status")
def update_financial_status(
    request: Request,
    kind: str,
    item_id: str,
    item_status: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
):
    admin = (
        request.app.state.web.require_owner(request)
        if kind == "payroll"
        else request.app.state.web.require_admin(request)
    )
    request.app.state.web.require_csrf(request, csrf)
    table = {"invoice": "invoices", "payroll": "payroll_payments"}.get(kind)
    if not table:
        raise HTTPException(status_code=404, detail="Unknown financial record")
    if kind == "payroll" and request.app.state.payroll_delivery.configured:
        raise HTTPException(
            status_code=409,
            detail="Configured payroll providers manage payment status by reconciliation",
        )
    try:
        request.app.state.database.set_financial_status(table, item_id, item_status)
        if kind == "invoice":
            # The printable document is an immutable authenticated snapshot. Keep
            # it aligned with operational state whenever an invoice is sent,
            # paid, voided, or marked overdue.
            request.app.state.invoice_vault.seal(item_id)
    except (ValueError, InvoiceVaultError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        admin["id"], f"{kind}.status_changed", kind, item_id, item_status
    )
    return RedirectResponse("/financials", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    request.app.state.web.require_admin(request)
    database = request.app.state.database
    return _page(
        request,
        "settings.html",
        settings=database.organization_settings(),
        server=request.app.state.settings,
        automatic_policies=database.list_automatic_tracking_policies(),
        automatic_members=[
            user
            for user in database.list_users()
            if user["enabled"] and user["role"] in {"admin", "manager", "member"}
        ],
        automatic_projects=database.list_trackable_projects(),
        github_integrations=database.list_github_integrations(),
        jira_integrations=database.list_jira_integrations(),
        asana_integrations=database.list_asana_integrations(),
        slack_integrations=database.list_slack_integrations(),
    )


@router.get("/settings/member-tracking", response_class=HTMLResponse)
def member_tracking_settings_page(
    request: Request,
    search: Annotated[str, Query(max_length=120)] = "",
    page: Annotated[int, Query(ge=1, le=20_000)] = 1,
):
    request.app.state.web.require_admin(request)
    page_size = 50
    members, total = request.app.state.database.list_member_tracking_settings(
        search, limit=page_size, offset=(page - 1) * page_size
    )

    def page_url(number: int) -> str:
        query = urlencode({"search": search, "page": number})
        return f"/settings/member-tracking?{query}"

    return _page(
        request,
        "member_tracking_settings.html",
        members=members,
        search=search,
        page=page,
        total=total,
        previous_url=page_url(page - 1) if page > 1 else "",
        next_url=page_url(page + 1) if page * page_size < total else "",
    )


def _inherited_boolean(value: str) -> bool | None:
    if value == "inherit":
        return None
    if value == "on":
        return True
    if value == "off":
        return False
    raise HTTPException(status_code=422, detail="Invalid tracking policy switch")


@router.post("/settings/member-tracking/{user_id}")
async def update_member_tracking_settings(
    request: Request,
    user_id: str,
):
    actor = request.app.state.web.require_admin(request)
    form = await request.form()
    request.app.state.web.require_csrf(request, str(form.get("csrf", "")))

    def policy_value(name: str) -> str:
        value = str(form.get(name, "inherit"))
        if len(value) > 20:
            raise HTTPException(status_code=422, detail="Invalid tracking policy value")
        return value

    screenshot_frequency = policy_value("screenshot_frequency")
    screenshot_blur = policy_value("screenshot_blur")
    track_apps = policy_value("track_apps")
    track_urls = policy_value("track_urls")
    allowed_apps = policy_value("allowed_apps")
    idle_timeout_minutes = policy_value("idle_timeout_minutes")
    allow_screenshot_delete = policy_value("allow_screenshot_delete")
    try:
        user_id = str(UUID(user_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Member not found") from exc
    try:
        frequency = (
            None if screenshot_frequency == "inherit" else int(screenshot_frequency)
        )
        idle_timeout = (
            None
            if idle_timeout_minutes in {"", "inherit"}
            else int(idle_timeout_minutes)
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="Tracking values must be numbers or inherited"
        ) from exc
    database = request.app.state.database
    target = database.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="Member not found")
    before = database.effective_tracking_settings(user_id)
    try:
        database.update_member_tracking_settings(
            user_id,
            {
                "screenshot_frequency": frequency,
                "screenshot_blur": _inherited_boolean(screenshot_blur),
                "track_apps": _inherited_boolean(track_apps),
                "track_urls": _inherited_boolean(track_urls),
                "allowed_apps": None if allowed_apps == "inherit" else allowed_apps,
                "idle_timeout_minutes": idle_timeout,
                "allow_screenshot_delete": _inherited_boolean(allow_screenshot_delete),
            },
            actor["id"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    after = database.effective_tracking_settings(user_id)
    changed = [
        f"{key}={before[key]}→{after[key]}"
        for key in database.TRACKING_OVERRIDE_FIELDS
        if before[key] != after[key]
    ]
    database.add_audit_event(
        actor["id"],
        "tracking_policy.updated",
        "user",
        user_id,
        ", ".join(changed) if changed else "inheritance updated",
    )
    return RedirectResponse(
        "/settings/member-tracking", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/it-management", response_class=HTMLResponse)
def it_management_page(request: Request):
    request.app.state.web.require_it_manager(request)
    return _page(
        request,
        "it_management.html",
        settings=request.app.state.database.organization_settings(),
        server=request.app.state.settings,
    )


@router.post("/it-management")
async def update_it_management(request: Request):
    actor = request.app.state.web.require_it_manager(request)
    form = await request.form()
    request.app.state.web.require_csrf(request, str(form.get("csrf", "")))
    sso_provider, sso_domain = _sso_form_values(request, form)
    try:
        screenshot_frequency = int(form.get("screenshot_frequency", 2))
        idle_timeout_minutes = int(form.get("idle_timeout_minutes", 20))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail="Tracking values must be numbers"
        ) from exc
    if screenshot_frequency not in range(4):
        raise HTTPException(
            status_code=422, detail="Screenshot frequency must be between 0 and 3"
        )
    if not 1 <= idle_timeout_minutes <= 1440:
        raise HTTPException(
            status_code=422, detail="Idle timeout must be between 1 and 1440 minutes"
        )
    allowed_apps = str(
        form.get(
            "allowed_apps",
            request.app.state.database.organization_settings()["allowed_apps"],
        )
    )
    if allowed_apps not in {"all", "desktop_only"}:
        raise HTTPException(status_code=422, detail="Invalid allowed apps policy")
    request.app.state.database.update_organization_settings(
        {
            "screenshot_frequency": screenshot_frequency,
            "screenshot_blur": "screenshot_blur" in form,
            "track_apps": "track_apps" in form,
            "track_urls": "track_urls" in form,
            "allowed_apps": allowed_apps,
            "idle_timeout_minutes": idle_timeout_minutes,
            "sso_provider": sso_provider,
            "sso_domain": sso_domain,
        }
    )
    request.app.state.database.add_audit_event(
        actor["id"],
        "it_management.updated",
        "organization",
        None,
        "tracking and identity",
    )
    return RedirectResponse("/it-management", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/settings")
async def update_settings(request: Request):
    admin = request.app.state.web.require_admin(request)
    form = await request.form()
    request.app.state.web.require_csrf(request, str(form.get("csrf", "")))

    def checked(key: str) -> bool:
        return key in form

    def integer(key: str, default: int) -> int:
        try:
            return int(form.get(key, default))
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail=f"{key.replace('_', ' ').title()} is invalid"
            ) from exc

    sso_provider, sso_domain = _sso_form_values(request, form)
    allowed_apps = str(
        form.get(
            "allowed_apps",
            request.app.state.database.organization_settings()["allowed_apps"],
        )
    )
    if allowed_apps not in {"all", "desktop_only"}:
        raise HTTPException(status_code=422, detail="Invalid allowed apps policy")
    values = {
        "screenshot_frequency": integer("screenshot_frequency", 2),
        "screenshot_blur": checked("screenshot_blur"),
        "track_apps": checked("track_apps"),
        "track_urls": checked("track_urls"),
        "allowed_apps": allowed_apps,
        "allow_manual_time": checked("allow_manual_time"),
        "require_time_approval": checked("require_time_approval"),
        "allow_screenshot_delete": checked("allow_screenshot_delete"),
        "require_edit_reason": checked("require_edit_reason"),
        "allow_keep_idle": checked("allow_keep_idle"),
        "require_two_factor": checked("require_two_factor"),
        "sso_provider": sso_provider,
        "sso_domain": sso_domain,
        "idle_timeout_minutes": integer("idle_timeout_minutes", 20),
    }
    if admin["role"] == "admin":
        values.update(
            {
                "name": str(form.get("name", "Dayfinch Workspace"))[:120],
                "address": str(form.get("address", ""))[:500],
                "tax_id": str(form.get("tax_id", ""))[:120],
                "timezone": str(form.get("timezone", "UTC"))[:80],
                "currency": str(form.get("currency", "USD"))[:3].upper(),
                "pay_period": str(form.get("pay_period", "weekly")),
                "overtime_enabled": checked("overtime_enabled"),
                "weekly_overtime_minutes": integer("weekly_overtime_minutes", 2400),
                "overtime_multiplier": str(form.get("overtime_multiplier", "1.5")),
                "retention_days": integer("retention_days", 90),
            }
        )
    try:
        request.app.state.database.update_organization_settings(values)
    except (ValueError, InvalidOperation) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        admin["id"], "settings.updated", "organization", None, "tracking policy"
    )
    return RedirectResponse("/settings", status_code=status.HTTP_303_SEE_OTHER)


def _automatic_policy_values(form) -> tuple[str, str, str, bool, dict, list[str]]:
    rule_type = str(form.get("rule_type", "fixed_schedule"))
    if rule_type not in {"fixed_schedule", "shifts"}:
        raise HTTPException(status_code=422, detail="Invalid automatic tracking rule")
    days = list(dict.fromkeys(str(value) for value in form.getlist("day")))
    allowed_days = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    if any(day not in allowed_days for day in days):
        raise HTTPException(status_code=422, detail="Invalid schedule day")
    schedule = {}
    if rule_type == "fixed_schedule":
        start_time = str(form.get("start_time", "09:00"))
        end_time = str(form.get("end_time", "17:00"))
        try:
            datetime.strptime(start_time, "%H:%M")
            datetime.strptime(end_time, "%H:%M")
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="Use valid start and end times"
            ) from exc
        if start_time == end_time:
            raise HTTPException(
                status_code=422, detail="Start and end times must differ"
            )
        schedule = {day: [{"start": start_time, "end": end_time}] for day in days}
    return (
        str(form.get("name", "")),
        rule_type,
        str(form.get("project_id", "")),
        "wait_for_activity" in form,
        schedule,
        [str(value) for value in form.getlist("user_id")][:500],
    )


@router.post("/settings/automatic-tracking")
async def create_automatic_tracking_policy(request: Request):
    admin = request.app.state.web.require_admin(request)
    form = await request.form()
    request.app.state.web.require_csrf(request, str(form.get("csrf", "")))
    values = _automatic_policy_values(form)
    try:
        policy_id = request.app.state.database.create_automatic_tracking_policy(
            *values,
            admin["id"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        admin["id"],
        "automatic_tracking.created",
        "automatic_tracking_policy",
        policy_id,
        values[1],
    )
    return RedirectResponse("/settings", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/settings/automatic-tracking/{policy_id}", response_class=HTMLResponse)
def edit_automatic_tracking_policy_page(request: Request, policy_id: str):
    request.app.state.web.require_admin(request)
    database = request.app.state.database
    policy = database.get_automatic_tracking_policy(policy_id)
    if not policy:
        raise HTTPException(
            status_code=404, detail="Automatic tracking policy not found"
        )
    intervals = [
        interval
        for day in policy["schedule"].values()
        for interval in day
        if isinstance(interval, dict)
    ]
    return _page(
        request,
        "automatic_tracking_policy.html",
        policy=policy,
        selected_users={assignment["user_id"] for assignment in policy["assignments"]},
        selected_days=set(policy["schedule"]),
        schedule_start=(intervals[0].get("start", "09:00") if intervals else "09:00"),
        schedule_end=(intervals[0].get("end", "17:00") if intervals else "17:00"),
        members=[
            user
            for user in database.list_users()
            if user["enabled"] and user["role"] in {"admin", "manager", "member"}
        ],
        projects=database.list_trackable_projects(),
    )


@router.post("/settings/automatic-tracking/{policy_id}")
async def update_automatic_tracking_policy(request: Request, policy_id: str):
    admin = request.app.state.web.require_admin(request)
    form = await request.form()
    request.app.state.web.require_csrf(request, str(form.get("csrf", "")))
    values = _automatic_policy_values(form)
    try:
        updated = request.app.state.database.update_automatic_tracking_policy(
            policy_id, *values
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(
            status_code=404, detail="Automatic tracking policy not found"
        )
    request.app.state.database.add_audit_event(
        admin["id"],
        "automatic_tracking.updated",
        "automatic_tracking_policy",
        policy_id,
        values[1],
    )
    return RedirectResponse("/settings", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/settings/automatic-tracking/{policy_id}/delete")
def delete_automatic_tracking_policy(
    request: Request, policy_id: str, csrf: Annotated[str, Form()]
):
    admin = request.app.state.web.require_admin(request)
    request.app.state.web.require_csrf(request, csrf)
    if not request.app.state.database.delete_automatic_tracking_policy(policy_id):
        raise HTTPException(
            status_code=404, detail="Automatic tracking policy not found"
        )
    request.app.state.database.add_audit_event(
        admin["id"],
        "automatic_tracking.deleted",
        "automatic_tracking_policy",
        policy_id,
    )
    return RedirectResponse("/settings", status_code=status.HTTP_303_SEE_OTHER)

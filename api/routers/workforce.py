from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Form, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from ..schemas import FieldTimerEvent, LocationPoint
from ..services.invoice_vault import InvoiceVaultError
from ..services.payments import PaymentDeliveryError

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


@router.post("/timer/start")
def start_web_timer(
    request: Request,
    project_id: Annotated[str, Form()],
    task_id: Annotated[str, Form()] = "",
    csrf: Annotated[str, Form()] = "",
):
    web, db = request.app.state.web, request.app.state.database
    user = web.require_user(request)
    web.require_csrf(request, csrf)
    if not web.can_access_project(user, project_id):
        raise HTTPException(status_code=403, detail="Project is not assigned")
    device = db.web_timer_device(user["id"], project_id)
    db.sync_work_session(device, "active", task_id or None, project_id)
    db.add_audit_event(user["id"], "timer.started", "project", project_id, task_id)
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/timer/stop")
def stop_web_timer(request: Request, csrf: Annotated[str, Form()]):
    web, db = request.app.state.web, request.app.state.database
    user = web.require_user(request)
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
    user = web.require_user(request)
    web.require_csrf(request, csrf)
    active = db.active_timer(user["id"])
    if active:
        device = db.get_device(active["device_id"])
        if action == "pause":
            db.start_break(user["id"], active["id"])
        else:
            db.stop_break(user["id"])
        db.sync_work_session(
            device,
            "paused" if action == "pause" else "active",
            active.get("task_id"),
            active.get("project_id"),
        )
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/activity", response_class=HTMLResponse)
def activity_page(
    request: Request, user_id: str = "", project_id: str = "", tab: str = "screenshots"
):
    user = request.app.state.web.require_user(request)
    scope = _scope(user)
    chosen_user = user_id if scope is None and user_id else scope
    db = request.app.state.database
    return _page(
        request,
        "activity.html",
        tab=tab,
        records=db.activity_feed(chosen_user, project_id or None),
        apps=db.usage_summary("active_app", chosen_user),
        urls=db.usage_summary("active_url", chosen_user),
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
    request.app.state.web.require_admin(request)
    return _page(
        request,
        "people.html",
        users=request.app.state.database.list_users(),
        teams=request.app.state.database.list_teams(),
    )


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
    csrf: Annotated[str, Form()] = "",
):
    actor = request.app.state.web.require_admin(request)
    request.app.state.web.require_csrf(request, csrf)
    try:
        request.app.state.database.set_user_profile(
            user_id,
            role,
            full_name,
            Decimal(pay_rate),
            Decimal(bill_rate),
            weekly_limit_minutes,
            daily_limit_minutes,
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
    team_id = request.app.state.database.create_team(name, lead_user_id or None)
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
    request.app.state.database.add_team_member(team_id, user_id)
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
    request.app.state.database.remove_team_member(team_id, user_id)
    return RedirectResponse("/people", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/time-entries", response_class=HTMLResponse)
def time_entries_page(request: Request):
    user = request.app.state.web.require_user(request)
    scope = _scope(user)
    db = request.app.state.database
    return _page(
        request,
        "time_entries.html",
        entries=db.list_manual_time(scope),
        projects=db.list_projects(user["id"] if scope else None),
        users=db.list_users() if scope is None else [],
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
    user = web.require_user(request)
    web.require_csrf(request, csrf)
    policy = db.organization_settings()
    if user["role"] not in {"admin", "manager"} and not policy["allow_manual_time"]:
        raise HTTPException(status_code=403, detail="Manual time is disabled")
    if not web.can_access_project(user, project_id):
        raise HTTPException(status_code=403, detail="Project is not assigned")
    if task_id:
        task = db.get_task(task_id)
        if not task or task["project_id"] != project_id:
            raise HTTPException(
                status_code=422, detail="Task does not belong to project"
            )
    owner = user_id if user["role"] in {"admin", "manager"} and user_id else user["id"]
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
    admin = request.app.state.web.require_admin(request)
    request.app.state.web.require_csrf(request, csrf)
    request.app.state.database.review_item(
        "manual_time_entries", entry_id, admin["id"], decision
    )
    return RedirectResponse("/time-entries", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/time-entries/bulk-review")
async def bulk_review_time_entries(request: Request):
    admin = request.app.state.web.require_admin(request)
    form = await request.form()
    request.app.state.web.require_csrf(request, str(form.get("csrf", "")))
    decision = str(form.get("decision", ""))
    item_ids = [str(value) for value in form.getlist("item_id")]
    if not item_ids:
        raise HTTPException(status_code=422, detail="Select at least one request")
    for item_id in item_ids[:200]:
        request.app.state.database.review_item(
            "manual_time_entries", item_id, admin["id"], decision
        )
    request.app.state.database.add_audit_event(
        admin["id"],
        "manual_time.bulk_reviewed",
        "manual_time_entry",
        None,
        f"{decision}:{len(item_ids[:200])}",
    )
    return RedirectResponse("/time-entries", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/schedules", response_class=HTMLResponse)
def schedules_page(request: Request):
    user = request.app.state.web.require_user(request)
    scope = _scope(user)
    db = request.app.state.database
    return _page(
        request,
        "schedules.html",
        shifts=db.list_shifts(scope),
        time_off=db.list_time_off(scope),
        users=db.list_users() if scope is None else [],
        projects=db.list_projects(scope),
        holidays=db.list_holidays(),
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
    user = request.app.state.web.require_user(request)
    db = request.app.state.database
    return _page(
        request,
        "field_tracker.html",
        projects=db.list_projects(user["id"]),
        active_timer=db.active_timer(user["id"]),
    )


@router.post("/field/location", status_code=201)
def field_location(
    request: Request,
    payload: LocationPoint,
    x_csrf_token: Annotated[str, Header()],
):
    web, db = request.app.state.web, request.app.state.database
    user = web.require_user(request)
    web.require_csrf(request, x_csrf_token)
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
        or next((p["id"] for p in db.list_projects(user["id"])), None)
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
        if event_fence.get("project_id") and web.can_access_project(
            user, event_fence["project_id"]
        ):
            db.sync_work_session(device, "active", None, event_fence["project_id"])
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
    user = web.require_user(request)
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
    if payload.action == "start":
        if not project_id or not web.can_access_project(user, project_id):
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
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
    admin = request.app.state.web.require_admin(request)
    request.app.state.web.require_csrf(request, csrf)
    try:
        request.app.state.database.add_shift(
            user_id,
            project_id or None,
            datetime.fromisoformat(starts_at),
            datetime.fromisoformat(ends_at),
            notes,
            admin["id"],
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
    user = request.app.state.web.require_user(request)
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
    admin = request.app.state.web.require_admin(request)
    request.app.state.web.require_csrf(request, csrf)
    request.app.state.database.review_item(
        "time_off_requests", item_id, admin["id"], decision
    )
    return RedirectResponse("/schedules", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/financials", response_class=HTMLResponse)
def financials_page(request: Request):
    user = request.app.state.web.require_user(request)
    scope = _scope(user)
    db = request.app.state.database
    return _page(
        request,
        "financials.html",
        finance=db.finance_summary() if scope is None else {},
        expenses=db.list_expenses(scope),
        projects=db.list_projects(scope),
        users=db.list_users() if scope is None else [],
        payroll_provider_configured=request.app.state.payroll_delivery.configured,
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
    user = request.app.state.web.require_user(request)
    request.app.state.web.require_csrf(request, csrf)
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
    admin = request.app.state.web.require_admin(request)
    request.app.state.web.require_csrf(request, csrf)
    request.app.state.database.review_item("expenses", item_id, admin["id"], decision)
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


@router.post("/payroll")
def add_payroll(
    request: Request,
    user_id: Annotated[str, Form()],
    period_start: Annotated[str, Form()],
    period_end: Annotated[str, Form()],
    currency: Annotated[str, Form()] = "USD",
    csrf: Annotated[str, Form()] = "",
):
    admin = request.app.state.web.require_admin(request)
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
    admin = request.app.state.web.require_admin(request)
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


@router.post("/api/v1/payroll/provider-callback", include_in_schema=False)
async def payroll_provider_callback(
    request: Request,
    x_dayfinch_signature: Annotated[str, Header()] = "",
):
    body = await request.body()
    try:
        payment = request.app.state.payroll_delivery.handle_callback(
            body, x_dayfinch_signature
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


@router.post("/financials/{kind}/{item_id}/status")
def update_financial_status(
    request: Request,
    kind: str,
    item_id: str,
    item_status: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
):
    admin = request.app.state.web.require_admin(request)
    request.app.state.web.require_csrf(request, csrf)
    table = {"invoice": "invoices", "payroll": "payroll_payments"}.get(kind)
    if not table:
        raise HTTPException(status_code=404, detail="Unknown financial record")
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
    return _page(
        request,
        "settings.html",
        settings=request.app.state.database.organization_settings(),
        server=request.app.state.settings,
        integrations=request.app.state.database.list_integrations(),
    )


@router.post("/settings")
async def update_settings(request: Request):
    admin = request.app.state.web.require_admin(request)
    form = await request.form()
    request.app.state.web.require_csrf(request, str(form.get("csrf", "")))

    def checked(key: str) -> bool:
        return key in form

    values = {
        "name": str(form.get("name", "Dayfinch Workspace"))[:120],
        "address": str(form.get("address", ""))[:500],
        "tax_id": str(form.get("tax_id", ""))[:120],
        "timezone": str(form.get("timezone", "UTC"))[:80],
        "currency": str(form.get("currency", "USD"))[:3].upper(),
        "screenshot_frequency": int(form.get("screenshot_frequency", 2)),
        "screenshot_blur": checked("screenshot_blur"),
        "track_apps": checked("track_apps"),
        "track_urls": checked("track_urls"),
        "allow_manual_time": checked("allow_manual_time"),
        "require_time_approval": checked("require_time_approval"),
        "allow_screenshot_delete": checked("allow_screenshot_delete"),
        "require_edit_reason": checked("require_edit_reason"),
        "allow_keep_idle": checked("allow_keep_idle"),
        "pay_period": str(form.get("pay_period", "weekly")),
        "require_two_factor": checked("require_two_factor"),
        "sso_provider": str(form.get("sso_provider", ""))[:80],
        "sso_domain": str(form.get("sso_domain", ""))[:255],
        "idle_timeout_minutes": int(form.get("idle_timeout_minutes", 20)),
        "retention_days": int(form.get("retention_days", 90)),
    }
    request.app.state.database.update_organization_settings(values)
    request.app.state.database.add_audit_event(
        admin["id"], "settings.updated", "organization", None, "tracking policy"
    )
    return RedirectResponse("/settings", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/integrations")
def add_integration(
    request: Request,
    provider: Annotated[str, Form()],
    display_name: Annotated[str, Form(min_length=1, max_length=120)],
    webhook_url: Annotated[str, Form(max_length=1000)] = "",
    csrf: Annotated[str, Form()] = "",
):
    admin = request.app.state.web.require_admin(request)
    request.app.state.web.require_csrf(request, csrf)
    integration_id = request.app.state.database.create_integration(
        provider, display_name, webhook_url, admin["id"]
    )
    request.app.state.database.add_audit_event(
        admin["id"], "integration.created", "integration", integration_id, provider
    )
    return RedirectResponse("/settings", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/integrations/{integration_id}/status")
def update_integration_status(
    request: Request,
    integration_id: str,
    enabled: Annotated[bool, Form()],
    csrf: Annotated[str, Form()],
):
    admin = request.app.state.web.require_admin(request)
    request.app.state.web.require_csrf(request, csrf)
    request.app.state.database.set_integration_enabled(integration_id, enabled)
    request.app.state.database.add_audit_event(
        admin["id"],
        "integration.status_changed",
        "integration",
        integration_id,
        str(enabled),
    )
    return RedirectResponse("/settings", status_code=status.HTTP_303_SEE_OTHER)

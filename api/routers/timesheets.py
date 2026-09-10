from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter(prefix="/timesheets", tags=["timesheets"])


def _timesheet_context(request: Request, user: dict, error: str | None) -> dict:
    database = request.app.state.database
    owner_filter = None if user["role"] in {"admin", "manager"} else user["id"]
    managed = (
        []
        if owner_filter is None
        else database.team_lead_members(user["id"], "approve_timesheets")
    )
    can_review = owner_filter is None or bool(managed)
    return request.app.state.web.page_context(
        request,
        timesheets=database.list_timesheets(
            owner_filter if not can_review else None,
            user_ids=[user["id"], *(member["id"] for member in managed)]
            if can_review and owner_filter is not None
            else None,
        ),
        error=error,
        can_review_timesheets=can_review,
        can_view_timesheet_rates=user["role"] in {"admin", "manager"},
    )


@router.get("", response_class=HTMLResponse)
def timesheets_page(request: Request):
    web = request.app.state.web
    redirect = web.user_or_login(request)
    if redirect:
        return redirect
    user = web.require_user(request)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="timesheets.html",
        context=_timesheet_context(request, user, None),
    )


@router.post("")
def submit_timesheet(
    request: Request,
    period_start: Annotated[str, Form()],
    period_end: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
):
    web = request.app.state.web
    user = web.require_worker(request)
    web.require_csrf(request, csrf)
    try:
        request.app.state.timesheets.submit(user, period_start, period_end)
    except ValueError as exc:
        return request.app.state.templates.TemplateResponse(
            request=request,
            name="timesheets.html",
            context=_timesheet_context(request, user, str(exc)),
            status_code=400,
        )
    return RedirectResponse("/timesheets", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{timesheet_id}/submit")
def submit_generated_timesheet(
    request: Request, timesheet_id: str, csrf: Annotated[str, Form()]
):
    web = request.app.state.web
    user = web.require_worker(request)
    web.require_csrf(request, csrf)
    sheet = request.app.state.database.get_timesheet(timesheet_id)
    if not sheet or sheet["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Timesheet not found")
    try:
        request.app.state.timesheets.submit(
            user, str(sheet["period_start"]), str(sheet["period_end"])
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse("/timesheets", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{timesheet_id}/review")
def review_timesheet(
    request: Request,
    timesheet_id: str,
    decision: Annotated[str, Form()],
    note: Annotated[str, Form(max_length=500)] = "",
    csrf: Annotated[str, Form()] = "",
):
    web = request.app.state.web
    reviewer = web.require_user(request)
    web.require_csrf(request, csrf)
    sheet = request.app.state.database.get_timesheet(timesheet_id)
    if not sheet or (
        reviewer["role"] not in {"admin", "manager"}
        and not request.app.state.database.team_lead_can_manage_user(
            reviewer["id"], sheet["user_id"], "approve_timesheets"
        )
    ):
        raise HTTPException(status_code=404, detail="Timesheet not found")
    try:
        request.app.state.timesheets.review(reviewer, timesheet_id, decision, note)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse("/timesheets", status_code=status.HTTP_303_SEE_OTHER)

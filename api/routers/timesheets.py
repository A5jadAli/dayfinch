from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse


router = APIRouter(prefix="/timesheets", tags=["timesheets"])


@router.get("", response_class=HTMLResponse)
def timesheets_page(request: Request):
    web = request.app.state.web
    redirect = web.user_or_login(request)
    if redirect:
        return redirect
    user = web.require_user(request)
    owner_filter = None if user["role"] == "admin" else user["id"]
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="timesheets.html",
        context=web.page_context(
            request,
            timesheets=request.app.state.database.list_timesheets(owner_filter),
            error=None,
        ),
    )


@router.post("")
def submit_timesheet(
    request: Request,
    period_start: Annotated[str, Form()],
    period_end: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
):
    web = request.app.state.web
    user = web.require_user(request)
    web.require_csrf(request, csrf)
    try:
        request.app.state.timesheets.submit(user, period_start, period_end)
    except ValueError as exc:
        owner_filter = None if user["role"] == "admin" else user["id"]
        return request.app.state.templates.TemplateResponse(
            request=request,
            name="timesheets.html",
            context=web.page_context(
                request,
                timesheets=request.app.state.database.list_timesheets(owner_filter),
                error=str(exc),
            ),
            status_code=400,
        )
    return RedirectResponse("/timesheets", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{timesheet_id}/review")
def review_timesheet(
    request: Request,
    timesheet_id: str,
    decision: Annotated[str, Form()],
    note: Annotated[str, Form(max_length=500)],
    csrf: Annotated[str, Form()],
):
    web = request.app.state.web
    admin = web.require_admin(request)
    web.require_csrf(request, csrf)
    try:
        request.app.state.timesheets.review(admin, timesheet_id, decision, note)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse("/timesheets", status_code=status.HTTP_303_SEE_OTHER)

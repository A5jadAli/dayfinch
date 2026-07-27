from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter(tags=["devices"])


@router.post("/devices", response_class=HTMLResponse)
def enroll_device(
    request: Request,
    name: Annotated[str, Form(min_length=1, max_length=100)],
    project_id: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
):
    web = request.app.state.web
    database = request.app.state.database
    user = web.require_user(request)
    web.require_csrf(request, csrf)
    project = database.get_project(project_id)
    if (
        not project
        or not project["enabled"]
        or not web.can_access_project(user, project_id)
    ):
        raise HTTPException(
            status_code=403, detail="Choose a project assigned to your account"
        )
    device, raw_token = database.create_device(name, user["id"], project_id)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="enrollment.html",
        context=web.page_context(request, device=device, raw_token=raw_token),
    )


@router.get("/devices/{device_id}", response_class=HTMLResponse)
def device_timeline(request: Request, device_id: str):
    web = request.app.state.web
    redirect = web.user_or_login(request)
    if redirect:
        return redirect
    user = web.require_user(request)
    database = request.app.state.database
    device = database.get_device(device_id)
    if not device or not web.can_access_device(user, device):
        raise HTTPException(status_code=404, detail="Device not found")
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="device.html",
        context=web.page_context(
            request, device=device, records=database.list_records(device_id)
        ),
    )


@router.post("/devices/{device_id}/enabled")
def change_device_state(
    request: Request,
    device_id: str,
    enabled: Annotated[int, Form(ge=0, le=1)],
    csrf: Annotated[str, Form()],
):
    web = request.app.state.web
    database = request.app.state.database
    user = web.require_user(request)
    web.require_csrf(request, csrf)
    device = database.get_device(device_id)
    if not device or not web.can_access_device(user, device):
        raise HTTPException(status_code=404, detail="Device not found")
    database.set_device_enabled(device_id, bool(enabled))
    database.add_audit_event(
        user["id"],
        "device.enabled" if enabled else "device.revoked",
        "device",
        device_id,
    )
    return RedirectResponse(
        f"/devices/{device_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/screenshots/{record_id}")
def screenshot(request: Request, record_id: str) -> Response:
    web = request.app.state.web
    database = request.app.state.database
    user = web.require_user(request)
    record = database.get_record(record_id)
    if not record or not web.can_access_record(user, record):
        raise HTTPException(status_code=404, detail="Screenshot not found")
    try:
        content = request.app.state.storage.read(record["screenshot_path"])
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Screenshot file missing") from exc
    database.add_audit_event(
        user["id"], "screenshot.viewed", "activity_record", record_id
    )
    return Response(
        content=content.data,
        media_type=content.content_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/screenshots/{record_id}/delete")
def delete_screenshot(
    request: Request,
    record_id: str,
    csrf: Annotated[str, Form()],
):
    web = request.app.state.web
    database = request.app.state.database
    user = web.require_user(request)
    web.require_csrf(request, csrf)
    record = database.get_record(record_id)
    if not record or not web.can_access_record(user, record):
        raise HTTPException(status_code=404, detail="Screenshot not found")
    request.app.state.storage.delete(
        record["screenshot_path"], record.get("storage_version_id")
    )
    database.delete_record(record_id)
    database.add_audit_event(
        user["id"],
        "activity_record.deleted",
        "device",
        record["device_id"],
        record_id,
    )
    return RedirectResponse(
        f"/devices/{record['device_id']}", status_code=status.HTTP_303_SEE_OTHER
    )

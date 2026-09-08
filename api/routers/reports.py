import csv
import io
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

router = APIRouter(prefix="/reports", tags=["reports"])


def _csv_response(filename: str, fields: list[str], rows: list[dict]) -> Response:
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        stream.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Cache-Control": "private, no-store",
        },
    )


@router.get("")
def reports_page(request: Request):
    web = request.app.state.web
    web.require_admin(request)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="reports.html",
        context=web.page_context(
            request,
            scheduled_reports=request.app.state.database.list_scheduled_reports(),
        ),
    )


@router.post("/scheduled")
def schedule_report(
    request: Request,
    name: Annotated[str, Form(min_length=1, max_length=120)],
    report_type: Annotated[str, Form()],
    frequency: Annotated[str, Form()],
    recipients: Annotated[str, Form(max_length=1000)],
    csrf: Annotated[str, Form()],
):
    web = request.app.state.web
    admin = web.require_admin(request)
    web.require_csrf(request, csrf)
    request.app.state.database.create_scheduled_report(
        name, report_type, frequency, recipients, admin["id"]
    )
    return RedirectResponse("/reports", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/scheduled/{report_id}/status")
def schedule_report_status(
    request: Request,
    report_id: str,
    enabled: Annotated[bool, Form()],
    csrf: Annotated[str, Form()],
):
    web = request.app.state.web
    web.require_admin(request)
    web.require_csrf(request, csrf)
    request.app.state.database.set_scheduled_report_enabled(report_id, enabled)
    return RedirectResponse("/reports", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/scheduled/{report_id}/delete")
def delete_scheduled_report(
    request: Request,
    report_id: str,
    csrf: Annotated[str, Form()],
):
    web = request.app.state.web
    web.require_admin(request)
    web.require_csrf(request, csrf)
    request.app.state.database.delete_scheduled_report(report_id)
    return RedirectResponse("/reports", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/time.csv")
def time_report_csv(request: Request) -> Response:
    request.app.state.web.require_admin(request)
    fields = [
        "email",
        "project",
        "task",
        "started_at",
        "ended_at",
        "status",
        "seconds",
        "time_type",
    ]
    return _csv_response(
        "dayfinch-time.csv", fields, request.app.state.database.time_report()
    )


@router.get("/attendance.csv")
def attendance_report_csv(request: Request) -> Response:
    request.app.state.web.require_admin(request)
    fields = ["email", "starts_at", "ends_at", "project_name", "worked_seconds"]
    return _csv_response(
        "dayfinch-attendance.csv",
        fields,
        request.app.state.database.attendance_report(),
    )


@router.get("/expenses.csv")
def expense_report_csv(request: Request) -> Response:
    request.app.state.web.require_admin(request)
    fields = [
        "email",
        "incurred_on",
        "project_name",
        "category",
        "description",
        "amount",
        "currency",
        "status",
    ]
    return _csv_response(
        "dayfinch-expenses.csv", fields, request.app.state.database.list_expenses()
    )


@router.get("/activity.csv")
def activity_report_csv(request: Request, project_id: str | None = None) -> Response:
    database = request.app.state.database
    user = request.app.state.web.require_admin(request)
    if project_id and not database.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    rows = database.activity_report(project_id)
    fields = [
        "project",
        "member",
        "work_date",
        "intervals",
        "focused_seconds",
        "interactive_seconds",
        "keyboard_events",
        "mouse_clicks",
    ]
    database.add_audit_event(
        user["id"], "report.exported", "project", project_id, f"rows={len(rows)}"
    )
    return _csv_response("dayfinch-activity.csv", fields, rows)

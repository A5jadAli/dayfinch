from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from ..services.csv_export import csv_bytes
from ..services.pdf_export import PDFExportBusy, pdf_bytes
from ..services.quickbooks_iif import (
    MAX_IIF_ROWS,
    QuickBooksIIFError,
    quickbooks_timer_iif,
)
from ..services.zipstream import ZipEntry, stream_zip

router = APIRouter(prefix="/reports", tags=["reports"])


MAX_SCREENSHOT_EXPORT_DAYS = 90
MAX_SCREENSHOT_EXPORT_RECORDS = 1_000
MAX_SCREENSHOT_EXPORT_BYTES = 1 << 30
MAX_CUSTOM_REPORT_DAYS = 366
MAX_CUSTOM_REPORT_ROWS = 10_000
MAX_CUSTOM_PDF_ROWS = 1_000
MAX_QUICKBOOKS_EXPORT_DAYS = 366
MAX_SPECIALIZED_REPORT_DAYS = 366
MAX_SPECIALIZED_CSV_ROWS = 10_000
MAX_SPECIALIZED_PDF_ROWS = 1_000
MAX_AUDIT_PAGE_ROWS = 50
CUSTOM_REPORT_COLUMNS = {
    "work_date": "Date (UTC)",
    "email": "Member email",
    "full_name": "Member name",
    "project": "Project",
    "task": "Task",
    "client": "Client",
    "started_at": "Started",
    "ended_at": "Ended",
    "seconds": "Duration (seconds)",
    "time_type": "Time type",
    "status": "Status",
    "activity_percent": "Activity %",
    "keyboard_events": "Keyboard events",
    "mouse_clicks": "Mouse clicks",
}
CUSTOM_REPORT_GROUPS = {
    "none": "No grouping",
    "date": "Date",
    "member": "Member",
    "member_date": "Member and date",
    "project": "Project",
    "client": "Client",
    "task": "Project and task",
}
CUSTOM_REPORT_GROUP_KEYS = {
    "date": (("work_date",), ("work_date",)),
    "member": (("user_id",), ("full_name", "email")),
    "member_date": (
        ("user_id", "work_date"),
        ("work_date", "full_name", "email"),
    ),
    "project": (("project_id",), ("project",)),
    "client": (("client",), ("client",)),
    "task": (("project_id", "task_id"), ("project", "task")),
}
DEFAULT_CUSTOM_REPORT_COLUMNS = [
    "email",
    "project",
    "task",
    "started_at",
    "ended_at",
    "seconds",
    "time_type",
    "activity_percent",
]

AUDIT_REPORT_FIELDS = [
    "occurred_at",
    "author",
    "action",
    "target_type",
    "affected_member",
    "details",
]
AUDIT_REPORT_LABELS = {
    "occurred_at": "Time (UTC)",
    "author": "Author",
    "action": "Action",
    "target_type": "Object",
    "affected_member": "Affected member",
    "details": "Detail",
}


def _csv_bytes(fields: list[str], rows: Iterable[dict]) -> bytes:
    # Backwards-compatible private alias for existing tests and router callers.
    return csv_bytes(fields, rows)


def _csv_response(filename: str, fields: list[str], rows: list[dict]) -> Response:
    return Response(
        _csv_bytes(fields, rows),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _report_date_window(
    date_from: date | None,
    date_to: date | None,
    *,
    default_days: int = 30,
) -> tuple[date, date, datetime, datetime]:
    today = datetime.now(UTC).date()
    end_date = date_to or today
    start_date = date_from or (end_date - timedelta(days=default_days - 1))
    if end_date < start_date:
        raise HTTPException(
            status_code=422, detail="date_to must be on or after date_from"
        )
    if end_date == date.max:
        raise HTTPException(
            status_code=422, detail="date_to is outside the supported range"
        )
    if (end_date - start_date).days + 1 > MAX_SPECIALIZED_REPORT_DAYS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Report periods are limited to {MAX_SPECIALIZED_REPORT_DAYS} days"
            ),
        )
    return (
        start_date,
        end_date,
        datetime.combine(start_date, time.min, tzinfo=UTC),
        datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=UTC),
    )


def _bounded_report_rows(
    rows: list[dict], *, maximum: int, export_format: str
) -> list[dict]:
    if len(rows) > maximum:
        raise HTTPException(
            status_code=422,
            detail=(
                f"The {export_format.upper()} report exceeds {maximum} rows; "
                "narrow the date or project filters"
            ),
        )
    return rows


def _pdf_response(
    *,
    filename: str,
    title: str,
    subtitle: str,
    fields: list[str],
    labels: dict[str, str],
    rows: list[dict],
) -> Response:
    try:
        payload = pdf_bytes(
            title=title,
            subtitle=subtitle,
            fields=fields,
            labels=labels,
            rows=rows,
        )
    except PDFExportBusy as exc:
        raise HTTPException(
            status_code=503,
            detail="PDF rendering is busy; retry shortly or export CSV",
            headers={"Retry-After": "2"},
        ) from exc
    return Response(
        payload,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _as_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (
        result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)
    )


def _screenshot_entries(
    request: Request, records: list[dict], truncated: bool
) -> Iterator[ZipEntry]:
    fields = [
        "record_id",
        "captured_at",
        "member",
        "project",
        "task",
        "activity_percent",
        "active_app",
        "active_url",
        "archive_path",
        "status",
    ]
    manifest: list[dict] = []
    total_bytes = 0
    now = datetime.now(UTC)
    for record in records:
        captured_at = _as_datetime(record["captured_at"])
        row = {
            "record_id": record["id"],
            "captured_at": captured_at.isoformat(),
            "member": record.get("full_name") or record.get("email") or "",
            "project": record.get("project_name") or "",
            "task": record.get("task_name") or "",
            "activity_percent": record.get("activity_percent", 0),
            "active_app": record.get("active_app") or "",
            "active_url": record.get("active_url") or "",
            "archive_path": "",
            "status": "missing",
        }
        path = record.get("screenshot_path")
        if path:
            try:
                content = request.app.state.storage.read(
                    path, record.get("storage_version_id")
                )
            except (FileNotFoundError, ValueError):
                pass
            else:
                extension = ".png" if content.content_type == "image/png" else ".jpg"
                archive_path = f"screenshots/{captured_at.date().isoformat()}/{record['id']}{extension}"
                if total_bytes + len(content.data) > MAX_SCREENSHOT_EXPORT_BYTES:
                    row["status"] = "skipped_size_limit"
                else:
                    total_bytes += len(content.data)
                    row["archive_path"] = archive_path
                    row["status"] = "included"
                    yield ZipEntry(archive_path, content.data, captured_at)
        manifest.append(row)
    if truncated:
        manifest.append(
            {
                "record_id": "",
                "captured_at": "",
                "member": "",
                "project": "",
                "task": "",
                "activity_percent": "",
                "active_app": "",
                "active_url": "",
                "archive_path": "",
                "status": f"truncated_after_{MAX_SCREENSHOT_EXPORT_RECORDS}_records",
            }
        )
    yield ZipEntry("manifest.csv", _csv_bytes(fields, manifest), now)


def _custom_report_scope(user: dict) -> tuple[str, str | None]:
    if user["role"] in {"admin", "manager"}:
        return "all", None
    if user["role"] == "viewer":
        return "project_member", user["id"]
    if user["role"] == "member":
        return "project_visibility", user["id"]
    return "personal", user["id"]


def _custom_report_values(
    request: Request,
    date_from: date | None,
    date_to: date | None,
    project_id: str,
    user_id: str,
    columns: list[str],
) -> tuple[dict, date, date, list[str]]:
    web = request.app.state.web
    database = request.app.state.database
    user = web.require_user(request)
    today = datetime.now(UTC).date()
    end_date = date_to or today
    start_date = date_from or (end_date - timedelta(days=6))
    if end_date < start_date:
        raise HTTPException(
            status_code=422, detail="date_to must be on or after date_from"
        )
    if end_date == date.max:
        raise HTTPException(
            status_code=422, detail="date_to is outside the supported range"
        )
    if (end_date - start_date).days + 1 > MAX_CUSTOM_REPORT_DAYS:
        raise HTTPException(
            status_code=422,
            detail=f"Custom reports are limited to {MAX_CUSTOM_REPORT_DAYS} days",
        )
    if project_id:
        project = database.get_project(project_id)
        if not project or not web.can_access_project(user, project_id):
            raise HTTPException(status_code=404, detail="Project not found")
    elevated = user["role"] in {"admin", "manager"}
    if user_id and (not elevated or not database.get_user_any(user_id)):
        raise HTTPException(status_code=404, detail="Member not found")
    chosen_columns = list(dict.fromkeys(columns or DEFAULT_CUSTOM_REPORT_COLUMNS))
    if any(column not in CUSTOM_REPORT_COLUMNS for column in chosen_columns):
        raise HTTPException(status_code=422, detail="Unsupported report column")
    return user, start_date, end_date, chosen_columns


def _run_custom_report(
    request: Request,
    user: dict,
    start_date: date,
    end_date: date,
    project_id: str,
    user_id: str,
    limit: int,
) -> list[dict]:
    scope_mode, scope_user_id = _custom_report_scope(user)
    return request.app.state.database.custom_time_report(
        datetime.combine(start_date, time.min, tzinfo=UTC),
        datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=UTC),
        user_id=user_id or None,
        project_id=project_id or None,
        scope_user_id=scope_user_id,
        scope_mode=scope_mode,
        limit=limit,
    )


def _validate_group(group_by: str) -> str:
    if group_by not in CUSTOM_REPORT_GROUPS:
        raise HTTPException(status_code=422, detail="Unsupported report grouping")
    return group_by


def _report_rows_with_dates(rows: list[dict]) -> list[dict]:
    result = []
    for row in rows:
        prepared = dict(row)
        prepared["work_date"] = _as_datetime(row["started_at"]).date().isoformat()
        result.append(prepared)
    return result


def _group_columns(columns: list[str], group_by: str) -> list[str]:
    if group_by == "none":
        return columns
    identity_fields = CUSTOM_REPORT_GROUP_KEYS[group_by][1]
    return [
        *identity_fields,
        *(field for field in columns if field not in identity_fields),
    ]


def _group_custom_report_rows(
    rows: list[dict], group_by: str, collapsed: bool
) -> list[dict]:
    prepared = _report_rows_with_dates(rows)
    if group_by == "none":
        return prepared
    key_fields = CUSTOM_REPORT_GROUP_KEYS[group_by][0]
    prepared.sort(
        key=lambda row: (
            tuple(str(row.get(field) or "").casefold() for field in key_fields)
            + (_as_datetime(row["started_at"]).isoformat(),)
        )
    )
    if not collapsed:
        return prepared

    groups: dict[tuple[str, ...], list[dict]] = {}
    for row in prepared:
        key = tuple(str(row.get(field) or "") for field in key_fields)
        groups.setdefault(key, []).append(row)

    aggregated: list[dict] = []
    additive = ("seconds", "keyboard_events", "mouse_clicks")
    text_fields = (
        "work_date",
        "email",
        "full_name",
        "project",
        "task",
        "client",
        "time_type",
        "status",
    )
    for group_rows in groups.values():
        total_seconds = sum(max(0, int(row.get("seconds") or 0)) for row in group_rows)
        result: dict = {
            field: sum(max(0, int(row.get(field) or 0)) for row in group_rows)
            for field in additive
        }
        result["started_at"] = min(
            (_as_datetime(row["started_at"]) for row in group_rows), default=None
        )
        result["ended_at"] = max(
            (_as_datetime(row["ended_at"]) for row in group_rows), default=None
        )
        result["activity_percent"] = (
            round(
                sum(
                    max(0, int(row.get("seconds") or 0))
                    * max(0, min(100, int(row.get("activity_percent") or 0)))
                    for row in group_rows
                )
                / total_seconds
            )
            if total_seconds
            else 0
        )
        for field in text_fields:
            values = {
                str(row.get(field))
                for row in group_rows
                if row.get(field) not in {None, ""}
            }
            result[field] = (
                ""
                if not values
                else next(iter(values))
                if len(values) == 1
                else "Multiple"
            )
        aggregated.append(result)
    return aggregated


@router.get("")
def reports_page(
    request: Request,
    date_from: date | None = None,
    date_to: date | None = None,
):
    web = request.app.state.web
    user = web.require_user(request)
    start_date, end_date, _, _ = _report_date_window(date_from, date_to)
    can_admin_reports = user["role"] in {"admin", "manager"}
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="reports.html",
        context=web.page_context(
            request,
            can_admin_reports=can_admin_reports,
            scheduled_reports=request.app.state.database.list_scheduled_reports()
            if can_admin_reports
            else [],
            report_date_from=start_date,
            report_date_to=end_date,
        ),
    )


@router.get("/quickbooks", response_class=HTMLResponse)
def quickbooks_export_page(request: Request):
    web = request.app.state.web
    web.require_admin(request)
    today = datetime.now(UTC).date()
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="quickbooks_export.html",
        context=web.page_context(
            request,
            configuration=request.app.state.database.quickbooks_export_configuration(),
            date_from=today - timedelta(days=6),
            date_to=today,
        ),
    )


@router.post("/quickbooks/settings")
def update_quickbooks_settings(
    request: Request,
    company_name: Annotated[str, Form(max_length=255)],
    company_create_time: Annotated[str, Form(max_length=20)],
    default_service_item: Annotated[str, Form(max_length=209)],
    timezone_name: Annotated[str, Form(max_length=80)],
    csrf: Annotated[str, Form()],
):
    web = request.app.state.web
    actor = web.require_admin(request)
    web.require_csrf(request, csrf)
    try:
        request.app.state.database.update_quickbooks_export_settings(
            company_name, company_create_time, default_service_item, timezone_name
        )
    except QuickBooksIIFError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"], "quickbooks.settings_updated", "organization", None
    )
    return RedirectResponse(
        "/reports/quickbooks", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/quickbooks/users/{user_id}")
def update_quickbooks_user_mapping(
    request: Request,
    user_id: str,
    quickbooks_name: Annotated[str, Form(max_length=209)],
    csrf: Annotated[str, Form()],
):
    web = request.app.state.web
    actor = web.require_admin(request)
    web.require_csrf(request, csrf)
    try:
        request.app.state.database.update_quickbooks_user_mapping(
            user_id, quickbooks_name
        )
    except QuickBooksIIFError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"], "quickbooks.user_mapping_updated", "user", user_id
    )
    return RedirectResponse(
        "/reports/quickbooks", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/quickbooks/projects/{project_id}")
def update_quickbooks_project_mapping(
    request: Request,
    project_id: str,
    customer_job: Annotated[str, Form(max_length=209)],
    class_name: Annotated[str, Form(max_length=159)],
    billable: Annotated[bool, Form()] = False,
    csrf: Annotated[str, Form()] = "",
):
    web = request.app.state.web
    actor = web.require_admin(request)
    web.require_csrf(request, csrf)
    try:
        request.app.state.database.update_quickbooks_project_mapping(
            project_id, customer_job, class_name, billable
        )
    except QuickBooksIIFError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"], "quickbooks.project_mapping_updated", "project", project_id
    )
    return RedirectResponse(
        "/reports/quickbooks", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/quickbooks/tasks/{task_id}")
def update_quickbooks_task_mapping(
    request: Request,
    task_id: str,
    service_item: Annotated[str, Form(max_length=209)],
    csrf: Annotated[str, Form()],
):
    web = request.app.state.web
    actor = web.require_admin(request)
    web.require_csrf(request, csrf)
    try:
        request.app.state.database.update_quickbooks_task_mapping(task_id, service_item)
    except QuickBooksIIFError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"], "quickbooks.task_mapping_updated", "task", task_id
    )
    return RedirectResponse(
        "/reports/quickbooks", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/quickbooks.iif")
def quickbooks_time_export(
    request: Request,
    date_from: date,
    date_to: date,
    approval_scope: str = "approved",
    user_id: str = "",
    project_id: str = "",
) -> Response:
    web = request.app.state.web
    database = request.app.state.database
    actor = web.require_admin(request)
    if approval_scope not in {"approved", "finalized"}:
        raise HTTPException(status_code=422, detail="Invalid approval scope")
    if date_to < date_from:
        raise HTTPException(
            status_code=422, detail="date_to must be on or after date_from"
        )
    if date_to == date.max:
        raise HTTPException(
            status_code=422, detail="date_to is outside the supported range"
        )
    if (date_to - date_from).days + 1 > MAX_QUICKBOOKS_EXPORT_DAYS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"QuickBooks exports are limited to {MAX_QUICKBOOKS_EXPORT_DAYS} days"
            ),
        )
    if user_id and not database.get_user_any(user_id):
        raise HTTPException(status_code=404, detail="Member not found")
    if project_id and not database.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    configuration = database.organization_settings()
    timezone_name = configuration["quickbooks_timezone"]
    try:
        accounting_timezone = ZoneInfo(timezone_name)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise HTTPException(
            status_code=422, detail="Configure a valid IANA QuickBooks timezone"
        ) from exc
    started_at = datetime.combine(date_from, time.min, accounting_timezone).astimezone(
        UTC
    )
    ended_at = datetime.combine(
        date_to + timedelta(days=1), time.min, accounting_timezone
    ).astimezone(UTC)
    rows = database.quickbooks_time_rows(
        started_at,
        ended_at,
        approved_only=approval_scope == "approved",
        user_id=user_id or None,
        project_id=project_id or None,
        timezone_name=timezone_name,
        limit=MAX_IIF_ROWS + 1,
    )
    if len(rows) > MAX_IIF_ROWS:
        raise HTTPException(
            status_code=422,
            detail=f"QuickBooks export exceeds {MAX_IIF_ROWS} grouped rows; narrow the filters",
        )
    try:
        export = quickbooks_timer_iif(
            company_name=configuration["quickbooks_company_name"],
            company_create_time=configuration["quickbooks_company_create_time"],
            default_service_item=configuration["quickbooks_default_service_item"],
            rows=rows,
        )
    except QuickBooksIIFError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    database.add_audit_event(
        actor["id"],
        "quickbooks.time_exported",
        "organization",
        None,
        (
            f"date_from={date_from};date_to={date_to};scope={approval_scope};"
            f"rows={export.row_count};source_seconds={export.source_seconds};"
            f"exported_minutes={export.exported_minutes};"
            f"skipped_seconds={export.skipped_seconds}"
        ),
    )
    filename = f"dayfinch-quickbooks-time-{date_from}-{date_to}.iif"
    return Response(
        export.data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/custom", response_class=HTMLResponse)
def custom_report_page(
    request: Request,
    date_from: date | None = None,
    date_to: date | None = None,
    project_id: str = "",
    user_id: str = "",
    columns: list[str] = Query(default=[]),
    group_by: str = "none",
    collapsed: bool = False,
    saved_id: str = "",
):
    database = request.app.state.database
    user = request.app.state.web.require_user(request)
    selected_saved = None
    if saved_id:
        selected_saved = database.get_saved_report_filter(saved_id, user["id"])
        if not selected_saved:
            raise HTTPException(status_code=404, detail="Saved report not found")
        configuration = selected_saved["configuration"]
        try:
            date_from = date.fromisoformat(configuration["date_from"])
            date_to = date.fromisoformat(configuration["date_to"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=409, detail="Saved report configuration is invalid"
            ) from exc
        project_id = str(configuration.get("project_id") or "")
        user_id = str(configuration.get("user_id") or "")
        columns = [str(value) for value in configuration.get("columns", [])]
        group_by = str(configuration.get("group_by") or "none")
        collapsed = bool(configuration.get("collapsed", False))
    group_by = _validate_group(group_by)
    user, start_date, end_date, chosen_columns = _custom_report_values(
        request, date_from, date_to, project_id, user_id, columns
    )
    rows = _run_custom_report(
        request,
        user,
        start_date,
        end_date,
        project_id,
        user_id,
        MAX_CUSTOM_REPORT_ROWS + 1 if collapsed and group_by != "none" else 201,
    )
    if collapsed and group_by != "none" and len(rows) > MAX_CUSTOM_REPORT_ROWS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"The report exceeds {MAX_CUSTOM_REPORT_ROWS} source rows; narrow "
                "the date or project filters before calculating grouped totals"
            ),
        )
    rows = _group_custom_report_rows(rows, group_by, collapsed)
    chosen_columns = _group_columns(chosen_columns, group_by)
    elevated = user["role"] in {"admin", "manager"}
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="custom_report.html",
        context=request.app.state.web.page_context(
            request,
            records=rows[:200],
            truncated=len(rows) > 200,
            columns=chosen_columns,
            column_options=CUSTOM_REPORT_COLUMNS,
            group_options=CUSTOM_REPORT_GROUPS,
            selected_group=group_by,
            collapsed=collapsed,
            date_from=start_date,
            date_to=end_date,
            selected_project=project_id,
            selected_user=user_id,
            projects=database.list_projects(None if elevated else user["id"]),
            users=database.list_users() if elevated else [],
            saved_reports=database.list_saved_report_filters(user["id"]),
            selected_saved=selected_saved,
        ),
    )


@router.get("/custom.csv")
def custom_report_csv(
    request: Request,
    date_from: date | None = None,
    date_to: date | None = None,
    project_id: str = "",
    user_id: str = "",
    columns: list[str] = Query(default=[]),
    group_by: str = "none",
    collapsed: bool = False,
) -> Response:
    group_by = _validate_group(group_by)
    user, start_date, end_date, chosen_columns = _custom_report_values(
        request, date_from, date_to, project_id, user_id, columns
    )
    rows = _run_custom_report(
        request,
        user,
        start_date,
        end_date,
        project_id,
        user_id,
        MAX_CUSTOM_REPORT_ROWS + 1,
    )
    if len(rows) > MAX_CUSTOM_REPORT_ROWS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"The report exceeds {MAX_CUSTOM_REPORT_ROWS} rows; narrow the date "
                "or project filters"
            ),
        )
    rows = _group_custom_report_rows(rows, group_by, collapsed)
    chosen_columns = _group_columns(chosen_columns, group_by)
    request.app.state.database.add_audit_event(
        user["id"],
        "custom_report.exported",
        "project",
        project_id or None,
        (
            f"rows={len(rows)};date_from={start_date};date_to={end_date};"
            f"group_by={group_by};collapsed={str(collapsed).lower()}"
        ),
    )
    return _csv_response(
        f"dayfinch-custom-{start_date}-{end_date}.csv", chosen_columns, rows
    )


@router.get("/custom.pdf")
def custom_report_pdf(
    request: Request,
    date_from: date | None = None,
    date_to: date | None = None,
    project_id: str = "",
    user_id: str = "",
    columns: list[str] = Query(default=[]),
    group_by: str = "none",
    collapsed: bool = False,
) -> Response:
    group_by = _validate_group(group_by)
    user, start_date, end_date, chosen_columns = _custom_report_values(
        request, date_from, date_to, project_id, user_id, columns
    )
    source_rows = _run_custom_report(
        request,
        user,
        start_date,
        end_date,
        project_id,
        user_id,
        MAX_CUSTOM_REPORT_ROWS + 1,
    )
    if len(source_rows) > MAX_CUSTOM_REPORT_ROWS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"The report exceeds {MAX_CUSTOM_REPORT_ROWS} source rows; narrow "
                "the date or project filters"
            ),
        )
    rows = _group_custom_report_rows(source_rows, group_by, collapsed)
    if len(rows) > MAX_CUSTOM_PDF_ROWS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"PDF reports are limited to {MAX_CUSTOM_PDF_ROWS} rendered rows; "
                "narrow the filters or collapse grouped totals"
            ),
        )
    chosen_columns = _group_columns(chosen_columns, group_by)
    grouping = CUSTOM_REPORT_GROUPS[group_by]
    try:
        payload = pdf_bytes(
            title="Dayfinch time and activity report",
            subtitle=(
                f"{start_date} through {end_date} (UTC) · Grouping: {grouping} · "
                f"{'Collapsed totals' if collapsed and group_by != 'none' else 'Expanded rows'}"
            ),
            fields=chosen_columns,
            labels=CUSTOM_REPORT_COLUMNS,
            rows=rows,
        )
    except PDFExportBusy as exc:
        raise HTTPException(
            status_code=503,
            detail="PDF rendering is busy; retry shortly or export CSV",
            headers={"Retry-After": "2"},
        ) from exc
    request.app.state.database.add_audit_event(
        user["id"],
        "custom_report.pdf_exported",
        "project",
        project_id or None,
        (
            f"rows={len(rows)};source_rows={len(source_rows)};date_from={start_date};"
            f"date_to={end_date};group_by={group_by};"
            f"collapsed={str(collapsed).lower()}"
        ),
    )
    filename = f"dayfinch-custom-{start_date}-{end_date}.pdf"
    return Response(
        payload,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/custom/saved")
def save_custom_report(
    request: Request,
    name: Annotated[str, Form(min_length=1, max_length=120)],
    description: Annotated[str, Form(max_length=500)],
    date_from: Annotated[date, Form()],
    date_to: Annotated[date, Form()],
    project_id: Annotated[str, Form()] = "",
    user_id: Annotated[str, Form()] = "",
    columns: Annotated[list[str] | None, Form()] = None,
    group_by: Annotated[str, Form()] = "none",
    collapsed: Annotated[bool, Form()] = False,
    csrf: Annotated[str, Form()] = "",
):
    web = request.app.state.web
    user = web.require_user(request)
    web.require_csrf(request, csrf)
    _, start_date, end_date, chosen_columns = _custom_report_values(
        request, date_from, date_to, project_id, user_id, columns or []
    )
    group_by = _validate_group(group_by)
    try:
        filter_id = request.app.state.database.create_saved_report_filter(
            user["id"],
            name,
            description,
            {
                "date_from": start_date.isoformat(),
                "date_to": end_date.isoformat(),
                "project_id": project_id or None,
                "user_id": user_id or None,
                "columns": chosen_columns,
                "group_by": group_by,
                "collapsed": collapsed,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        user["id"], "custom_report.saved", "saved_report_filter", filter_id
    )
    return RedirectResponse(
        f"/reports/custom?saved_id={filter_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/custom/saved/{filter_id}/delete")
def delete_custom_report(
    request: Request,
    filter_id: str,
    csrf: Annotated[str, Form()],
):
    web = request.app.state.web
    user = web.require_user(request)
    web.require_csrf(request, csrf)
    if not request.app.state.database.delete_saved_report_filter(filter_id, user["id"]):
        raise HTTPException(status_code=404, detail="Saved report not found")
    request.app.state.database.add_audit_event(
        user["id"], "custom_report.deleted", "saved_report_filter", filter_id
    )
    return RedirectResponse("/reports/custom", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/screenshots.zip")
def screenshot_report_zip(
    request: Request,
    user_id: str = "",
    project_id: str = "",
    date_from: date | None = None,
    date_to: date | None = None,
) -> StreamingResponse:
    web = request.app.state.web
    database = request.app.state.database
    user = web.require_user(request)
    elevated = user["role"] in {"admin", "manager"}

    if project_id:
        project = database.get_project(project_id)
        if not project or not web.can_access_project(user, project_id):
            raise HTTPException(status_code=404, detail="Project not found")
    if user_id and not elevated:
        raise HTTPException(status_code=404, detail="Member not found")
    if user_id and not database.get_user_any(user_id):
        raise HTTPException(status_code=404, detail="Member not found")

    today = datetime.now(UTC).date()
    end_date = date_to or today
    start_date = date_from or (end_date - timedelta(days=6))
    if end_date < start_date:
        raise HTTPException(
            status_code=422, detail="date_to must be on or after date_from"
        )
    if end_date == date.max:
        raise HTTPException(
            status_code=422, detail="date_to is outside the supported range"
        )
    if (end_date - start_date).days + 1 > MAX_SCREENSHOT_EXPORT_DAYS:
        raise HTTPException(
            status_code=422,
            detail=f"Screenshot exports are limited to {MAX_SCREENSHOT_EXPORT_DAYS} days",
        )

    chosen_user = user_id or None if elevated else user["id"]
    viewer_project_scope = user["id"] if user["role"] == "viewer" else None
    project_visibility_scope = user["id"] if user["role"] == "member" else None
    if viewer_project_scope or project_visibility_scope:
        chosen_user = None
    captured_from = datetime.combine(start_date, time.min, tzinfo=UTC)
    captured_to = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=UTC)
    records = database.activity_feed(
        chosen_user,
        project_id or None,
        viewer_project_scope,
        project_visibility_scope,
        MAX_SCREENSHOT_EXPORT_RECORDS + 1,
        captured_from,
        captured_to,
    )
    truncated = len(records) > MAX_SCREENSHOT_EXPORT_RECORDS
    records = records[:MAX_SCREENSHOT_EXPORT_RECORDS]
    database.add_audit_event(
        user["id"],
        "screenshots.exported",
        "project",
        project_id or None,
        (
            f"records={len(records)};date_from={start_date.isoformat()};"
            f"date_to={end_date.isoformat()};truncated={str(truncated).lower()}"
        ),
    )
    filename = f"dayfinch-screenshots-{start_date}-{end_date}.zip"
    return StreamingResponse(
        stream_zip(_screenshot_entries(request, records, truncated)),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/scheduled")
def schedule_report(
    request: Request,
    name: Annotated[str, Form(min_length=1, max_length=120)],
    report_type: Annotated[str, Form()],
    frequency: Annotated[str, Form()],
    recipients: Annotated[str, Form(max_length=1000)],
    csrf: Annotated[str, Form()],
    delivery_format: Annotated[str, Form()] = "csv",
    delivery_time: Annotated[time, Form()] = time(9, 0),
    schedule_weekday: Annotated[int, Form(ge=0, le=6)] = 0,
    schedule_month_day: Annotated[int, Form()] = 1,
    range_preset: Annotated[str, Form()] = "previous_period",
):
    web = request.app.state.web
    admin = web.require_admin(request)
    web.require_csrf(request, csrf)
    try:
        request.app.state.database.create_scheduled_report(
            name,
            report_type,
            frequency,
            recipients,
            admin["id"],
            delivery_format,
            delivery_time,
            schedule_weekday,
            schedule_month_day,
            range_preset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
    try:
        request.app.state.database.set_scheduled_report_enabled(report_id, enabled)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
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
    try:
        request.app.state.database.delete_scheduled_report(report_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse("/reports", status_code=status.HTTP_303_SEE_OTHER)


TIME_REPORT_FIELDS = [
    "email",
    "project",
    "task",
    "started_at",
    "ended_at",
    "status",
    "seconds",
    "time_type",
]
TIME_REPORT_LABELS = {
    "email": "Member email",
    "project": "Project",
    "task": "Task",
    "started_at": "Started (UTC)",
    "ended_at": "Ended (UTC)",
    "status": "Status",
    "seconds": "Duration",
    "time_type": "Time type",
}
ATTENDANCE_REPORT_FIELDS = [
    "email",
    "starts_at",
    "ends_at",
    "project_name",
    "worked_seconds",
]
ATTENDANCE_REPORT_LABELS = {
    "email": "Member email",
    "starts_at": "Shift start (UTC)",
    "ends_at": "Shift end (UTC)",
    "project_name": "Project",
    "worked_seconds": "Worked seconds",
}
EXPENSE_REPORT_FIELDS = [
    "email",
    "incurred_on",
    "project_name",
    "category",
    "description",
    "amount",
    "currency",
    "status",
]
EXPENSE_REPORT_LABELS = {
    "email": "Member email",
    "incurred_on": "Incurred on",
    "project_name": "Project",
    "category": "Category",
    "description": "Description",
    "amount": "Amount",
    "currency": "Currency",
    "status": "Status",
}
ACTIVITY_REPORT_FIELDS = [
    "project",
    "member",
    "work_date",
    "intervals",
    "focused_seconds",
    "interactive_seconds",
    "keyboard_events",
    "mouse_clicks",
]
ACTIVITY_REPORT_LABELS = {
    "project": "Project",
    "member": "Member email",
    "work_date": "Date (UTC)",
    "intervals": "Captured intervals",
    "focused_seconds": "Focused seconds",
    "interactive_seconds": "Interactive seconds",
    "keyboard_events": "Keyboard events",
    "mouse_clicks": "Mouse clicks",
}


def _audit_report_values(
    request: Request,
    date_from: date | None,
    date_to: date | None,
    actor_id: str,
    action: str,
    target_type: str,
    member_id: str,
) -> tuple[dict, date, date, datetime, datetime, dict]:
    actor = request.app.state.web.require_admin(request)
    start_date, end_date, started_at, ended_at = _report_date_window(date_from, date_to)
    database = request.app.state.database
    normalized_actor = actor_id.strip()
    normalized_member = member_id.strip()
    if normalized_actor and normalized_actor != "system":
        try:
            UUID(normalized_actor)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid audit author") from exc
        if not database.get_user_any(normalized_actor):
            raise HTTPException(status_code=404, detail="Audit author not found")
    if normalized_member:
        try:
            UUID(normalized_member)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="Invalid affected member"
            ) from exc
        if not database.get_user_any(normalized_member):
            raise HTTPException(status_code=404, detail="Affected member not found")
    normalized_action = action.strip()
    normalized_target = target_type.strip()
    if len(normalized_action) > 80 or len(normalized_target) > 40:
        raise HTTPException(status_code=422, detail="Invalid audit filter")
    filters = {
        "actor_user_id": normalized_actor if normalized_actor != "system" else None,
        "system_actor": normalized_actor == "system",
        "action": normalized_action,
        "target_type": normalized_target,
        "target_user_id": normalized_member or None,
    }
    return actor, start_date, end_date, started_at, ended_at, filters


def _audit_rows(
    request: Request,
    started_at: datetime,
    ended_at: datetime,
    filters: dict,
    *,
    limit: int,
    offset: int = 0,
) -> list[dict]:
    return request.app.state.database.audit_report(
        started_at, ended_at, **filters, limit=limit, offset=offset
    )


def _audit_specialized_export(
    request: Request,
    actor: dict,
    report_type: str,
    export_format: str,
    start_date: date,
    end_date: date,
    rows: list[dict],
    project_id: str | None = None,
) -> None:
    request.app.state.database.add_audit_event(
        actor["id"],
        "report.exported",
        "project",
        project_id,
        (
            f"type={report_type};format={export_format};rows={len(rows)};"
            f"date_from={start_date};date_to={end_date}"
        ),
    )


@router.get("/audit", response_class=HTMLResponse)
def audit_report_page(
    request: Request,
    date_from: date | None = None,
    date_to: date | None = None,
    actor_id: str = "",
    action: str = "",
    target_type: str = "",
    member_id: str = "",
    page: Annotated[int, Query(ge=1, le=200)] = 1,
):
    _actor, start_date, end_date, started_at, ended_at, filters = _audit_report_values(
        request,
        date_from,
        date_to,
        actor_id,
        action,
        target_type,
        member_id,
    )
    offset = (page - 1) * MAX_AUDIT_PAGE_ROWS
    rows = _audit_rows(
        request,
        started_at,
        ended_at,
        filters,
        limit=MAX_AUDIT_PAGE_ROWS + 1,
        offset=offset,
    )
    has_next = len(rows) > MAX_AUDIT_PAGE_ROWS
    rows = rows[:MAX_AUDIT_PAGE_ROWS]
    normalized_actor = (
        "system" if filters["system_actor"] else (filters["actor_user_id"] or "")
    )
    query_values = {
        "date_from": start_date.isoformat(),
        "date_to": end_date.isoformat(),
        "actor_id": normalized_actor,
        "action": filters["action"],
        "target_type": filters["target_type"],
        "member_id": filters["target_user_id"] or "",
    }

    def page_url(value: int) -> str:
        return "/reports/audit?" + urlencode({**query_values, "page": value})

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="audit_report.html",
        context=request.app.state.web.page_context(
            request,
            rows=rows,
            members=request.app.state.database.list_users(),
            filter_values=request.app.state.database.audit_report_filter_values(
                started_at, ended_at
            ),
            date_from=start_date,
            date_to=end_date,
            selected_actor=normalized_actor,
            selected_action=filters["action"],
            selected_target_type=filters["target_type"],
            selected_member=filters["target_user_id"] or "",
            page=page,
            previous_url=page_url(page - 1) if page > 1 else "",
            next_url=page_url(page + 1) if has_next else "",
            export_query=urlencode(query_values),
        ),
    )


@router.get("/audit.csv")
def audit_report_csv(
    request: Request,
    date_from: date | None = None,
    date_to: date | None = None,
    actor_id: str = "",
    action: str = "",
    target_type: str = "",
    member_id: str = "",
) -> Response:
    actor, start_date, end_date, started_at, ended_at, filters = _audit_report_values(
        request,
        date_from,
        date_to,
        actor_id,
        action,
        target_type,
        member_id,
    )
    rows = _bounded_report_rows(
        _audit_rows(
            request,
            started_at,
            ended_at,
            filters,
            limit=MAX_SPECIALIZED_CSV_ROWS + 1,
        ),
        maximum=MAX_SPECIALIZED_CSV_ROWS,
        export_format="csv",
    )
    _audit_specialized_export(
        request, actor, "audit", "csv", start_date, end_date, rows
    )
    return _csv_response(
        f"dayfinch-audit-{start_date}-{end_date}.csv", AUDIT_REPORT_FIELDS, rows
    )


@router.get("/audit.pdf")
def audit_report_pdf(
    request: Request,
    date_from: date | None = None,
    date_to: date | None = None,
    actor_id: str = "",
    action: str = "",
    target_type: str = "",
    member_id: str = "",
) -> Response:
    actor, start_date, end_date, started_at, ended_at, filters = _audit_report_values(
        request,
        date_from,
        date_to,
        actor_id,
        action,
        target_type,
        member_id,
    )
    rows = _bounded_report_rows(
        _audit_rows(
            request,
            started_at,
            ended_at,
            filters,
            limit=MAX_SPECIALIZED_PDF_ROWS + 1,
        ),
        maximum=MAX_SPECIALIZED_PDF_ROWS,
        export_format="pdf",
    )
    response = _pdf_response(
        filename=f"dayfinch-audit-{start_date}-{end_date}.pdf",
        title="Dayfinch audit log",
        subtitle=f"Organization changes {start_date} through {end_date} (UTC)",
        fields=AUDIT_REPORT_FIELDS,
        labels=AUDIT_REPORT_LABELS,
        rows=rows,
    )
    _audit_specialized_export(
        request, actor, "audit", "pdf", start_date, end_date, rows
    )
    return response


@router.get("/time.csv")
def time_report_csv(
    request: Request,
    date_from: date | None = None,
    date_to: date | None = None,
) -> Response:
    actor = request.app.state.web.require_admin(request)
    start_date, end_date, started_at, ended_at = _report_date_window(date_from, date_to)
    rows = _bounded_report_rows(
        request.app.state.database.time_report(
            started_at=started_at,
            ended_at=ended_at,
            limit=MAX_SPECIALIZED_CSV_ROWS + 1,
        ),
        maximum=MAX_SPECIALIZED_CSV_ROWS,
        export_format="csv",
    )
    _audit_specialized_export(request, actor, "time", "csv", start_date, end_date, rows)
    return _csv_response(
        f"dayfinch-time-{start_date}-{end_date}.csv", TIME_REPORT_FIELDS, rows
    )


@router.get("/time.pdf")
def time_report_pdf(
    request: Request,
    date_from: date | None = None,
    date_to: date | None = None,
) -> Response:
    actor = request.app.state.web.require_admin(request)
    start_date, end_date, started_at, ended_at = _report_date_window(date_from, date_to)
    rows = _bounded_report_rows(
        request.app.state.database.time_report(
            started_at=started_at,
            ended_at=ended_at,
            limit=MAX_SPECIALIZED_PDF_ROWS + 1,
        ),
        maximum=MAX_SPECIALIZED_PDF_ROWS,
        export_format="pdf",
    )
    response = _pdf_response(
        filename=f"dayfinch-time-{start_date}-{end_date}.pdf",
        title="Dayfinch time and activity report",
        subtitle=f"{start_date} through {end_date} (UTC)",
        fields=TIME_REPORT_FIELDS,
        labels=TIME_REPORT_LABELS,
        rows=rows,
    )
    _audit_specialized_export(request, actor, "time", "pdf", start_date, end_date, rows)
    return response


@router.get("/attendance.csv")
def attendance_report_csv(
    request: Request,
    date_from: date | None = None,
    date_to: date | None = None,
) -> Response:
    actor = request.app.state.web.require_admin(request)
    start_date, end_date, started_at, ended_at = _report_date_window(date_from, date_to)
    rows = _bounded_report_rows(
        request.app.state.database.attendance_report(
            started_at=started_at,
            ended_at=ended_at,
            limit=MAX_SPECIALIZED_CSV_ROWS + 1,
        ),
        maximum=MAX_SPECIALIZED_CSV_ROWS,
        export_format="csv",
    )
    _audit_specialized_export(
        request, actor, "attendance", "csv", start_date, end_date, rows
    )
    return _csv_response(
        f"dayfinch-attendance-{start_date}-{end_date}.csv",
        ATTENDANCE_REPORT_FIELDS,
        rows,
    )


@router.get("/attendance.pdf")
def attendance_report_pdf(
    request: Request,
    date_from: date | None = None,
    date_to: date | None = None,
) -> Response:
    actor = request.app.state.web.require_admin(request)
    start_date, end_date, started_at, ended_at = _report_date_window(date_from, date_to)
    rows = _bounded_report_rows(
        request.app.state.database.attendance_report(
            started_at=started_at,
            ended_at=ended_at,
            limit=MAX_SPECIALIZED_PDF_ROWS + 1,
        ),
        maximum=MAX_SPECIALIZED_PDF_ROWS,
        export_format="pdf",
    )
    response = _pdf_response(
        filename=f"dayfinch-attendance-{start_date}-{end_date}.pdf",
        title="Dayfinch attendance report",
        subtitle=f"Scheduled shifts overlapping {start_date} through {end_date} (UTC)",
        fields=ATTENDANCE_REPORT_FIELDS,
        labels=ATTENDANCE_REPORT_LABELS,
        rows=rows,
    )
    _audit_specialized_export(
        request, actor, "attendance", "pdf", start_date, end_date, rows
    )
    return response


@router.get("/expenses.csv")
def expense_report_csv(
    request: Request,
    date_from: date | None = None,
    date_to: date | None = None,
) -> Response:
    actor = request.app.state.web.require_admin(request)
    start_date, end_date, _, _ = _report_date_window(date_from, date_to)
    rows = _bounded_report_rows(
        request.app.state.database.list_expenses(
            incurred_from=start_date,
            incurred_to=end_date + timedelta(days=1),
            limit=MAX_SPECIALIZED_CSV_ROWS + 1,
        ),
        maximum=MAX_SPECIALIZED_CSV_ROWS,
        export_format="csv",
    )
    _audit_specialized_export(
        request, actor, "expenses", "csv", start_date, end_date, rows
    )
    return _csv_response(
        f"dayfinch-expenses-{start_date}-{end_date}.csv", EXPENSE_REPORT_FIELDS, rows
    )


@router.get("/expenses.pdf")
def expense_report_pdf(
    request: Request,
    date_from: date | None = None,
    date_to: date | None = None,
) -> Response:
    actor = request.app.state.web.require_admin(request)
    start_date, end_date, _, _ = _report_date_window(date_from, date_to)
    rows = _bounded_report_rows(
        request.app.state.database.list_expenses(
            incurred_from=start_date,
            incurred_to=end_date + timedelta(days=1),
            limit=MAX_SPECIALIZED_PDF_ROWS + 1,
        ),
        maximum=MAX_SPECIALIZED_PDF_ROWS,
        export_format="pdf",
    )
    response = _pdf_response(
        filename=f"dayfinch-expenses-{start_date}-{end_date}.pdf",
        title="Dayfinch expense report",
        subtitle=f"Expenses incurred {start_date} through {end_date} (UTC dates)",
        fields=EXPENSE_REPORT_FIELDS,
        labels=EXPENSE_REPORT_LABELS,
        rows=rows,
    )
    _audit_specialized_export(
        request, actor, "expenses", "pdf", start_date, end_date, rows
    )
    return response


@router.get("/team-invoices.csv")
def team_invoice_report_csv(request: Request) -> Response:
    database = request.app.state.database
    user = request.app.state.web.require_user(request)
    if user["role"] in {"admin", "manager"}:
        rows = database.list_team_invoices()
    else:
        managed = database.team_lead_members(user["id"], "manage_financials")
        rows = database.list_team_invoices(
            user_id=user["id"] if not managed else None,
            user_ids=[user["id"], *(member["id"] for member in managed)]
            if managed
            else None,
        )
    fields = [
        "number",
        "email",
        "full_name",
        "issued_on",
        "due_on",
        "status",
        "currency",
        "total",
        "paid_amount",
        "amount_due",
        "purchase_order",
    ]
    database.add_audit_event(
        user["id"],
        "team_invoice_report.exported",
        "team_invoice",
        None,
        f"rows={len(rows)}",
    )
    return _csv_response("dayfinch-team-invoices.csv", fields, rows)


@router.get("/activity.csv")
def activity_report_csv(
    request: Request,
    project_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> Response:
    database = request.app.state.database
    actor = request.app.state.web.require_admin(request)
    if project_id and not database.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    start_date, end_date, started_at, ended_at = _report_date_window(date_from, date_to)
    rows = _bounded_report_rows(
        database.activity_report(
            project_id,
            started_at=started_at,
            ended_at=ended_at,
            limit=MAX_SPECIALIZED_CSV_ROWS + 1,
        ),
        maximum=MAX_SPECIALIZED_CSV_ROWS,
        export_format="csv",
    )
    _audit_specialized_export(
        request,
        actor,
        "activity",
        "csv",
        start_date,
        end_date,
        rows,
        project_id,
    )
    return _csv_response(
        f"dayfinch-activity-{start_date}-{end_date}.csv",
        ACTIVITY_REPORT_FIELDS,
        rows,
    )


@router.get("/activity.pdf")
def activity_report_pdf(
    request: Request,
    project_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> Response:
    database = request.app.state.database
    actor = request.app.state.web.require_admin(request)
    if project_id and not database.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    start_date, end_date, started_at, ended_at = _report_date_window(date_from, date_to)
    rows = _bounded_report_rows(
        database.activity_report(
            project_id,
            started_at=started_at,
            ended_at=ended_at,
            limit=MAX_SPECIALIZED_PDF_ROWS + 1,
        ),
        maximum=MAX_SPECIALIZED_PDF_ROWS,
        export_format="pdf",
    )
    response = _pdf_response(
        filename=f"dayfinch-activity-{start_date}-{end_date}.pdf",
        title="Dayfinch activity report",
        subtitle=f"Captured activity {start_date} through {end_date} (UTC)",
        fields=ACTIVITY_REPORT_FIELDS,
        labels=ACTIVITY_REPORT_LABELS,
        rows=rows,
    )
    _audit_specialized_export(
        request,
        actor,
        "activity",
        "pdf",
        start_date,
        end_date,
        rows,
        project_id,
    )
    return response

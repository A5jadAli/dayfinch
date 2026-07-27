import csv
import io

from fastapi import APIRouter, HTTPException, Request, Response


router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/activity.csv")
def activity_report_csv(request: Request, project_id: str | None = None) -> Response:
    database = request.app.state.database
    user = request.app.state.web.require_admin(request)
    if project_id and not database.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    rows = database.activity_report(project_id)
    stream = io.StringIO()
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
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    database.add_audit_event(
        user["id"], "report.exported", "project", project_id, f"rows={len(rows)}"
    )
    return Response(
        stream.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=dayfinch-activity.csv",
            "Cache-Control": "private, no-store",
        },
    )

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["dashboard"])


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    web = request.app.state.web
    redirect = web.user_or_login(request)
    if redirect:
        return redirect
    user = web.require_user(request)
    database = request.app.state.database
    elevated = user["role"] in {"admin", "manager"}
    project_creation_teams = (
        [] if elevated else database.team_lead_teams(user["id"], "manage_projects")
    )
    viewer_scope = user["id"] if user["role"] == "viewer" else None
    owner_filter = None if elevated or viewer_scope else user["id"]
    project_filter = None if elevated else user["id"]
    projects = database.list_projects(project_filter)
    trackable_projects = database.list_trackable_projects(
        user["id"] if user["role"] == "member" else None
    )
    tracking_policy = database.effective_tracking_settings(user["id"])
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=web.page_context(
            request,
            devices=database.list_devices(owner_filter, project_member_id=viewer_scope),
            users=database.list_users() if elevated else [],
            projects=projects,
            trackable_projects=trackable_projects,
            summary=database.dashboard_summary(owner_filter, viewer_scope),
            active_timer=database.active_timer(user["id"]),
            can_track_time=bool(trackable_projects) and user["role"] != "viewer",
            web_timer_allowed=tracking_policy["allowed_apps"] == "all",
            can_manage=elevated,
            can_create_project=elevated or bool(project_creation_teams),
            project_creation_teams=project_creation_teams,
            can_invite=elevated,
        ),
    )

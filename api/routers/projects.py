from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse


router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("")
def create_project(
    request: Request,
    name: Annotated[str, Form(min_length=1, max_length=100)],
    description: Annotated[str, Form(max_length=500)],
    csrf: Annotated[str, Form()],
):
    web = request.app.state.web
    database = request.app.state.database
    admin = web.require_admin(request)
    web.require_csrf(request, csrf)
    try:
        project = database.create_project(name, description, admin["id"])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    database.add_project_member(project["id"], admin["id"])
    database.add_audit_event(
        admin["id"], "project.created", "project", project["id"], project["name"]
    )
    return RedirectResponse(
        f"/projects/{project['id']}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/{project_id}", response_class=HTMLResponse)
def project_page(request: Request, project_id: str):
    web = request.app.state.web
    redirect = web.user_or_login(request)
    if redirect:
        return redirect
    user = web.require_user(request)
    database = request.app.state.database
    project = database.get_project(project_id)
    if not project or not web.can_access_project(user, project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    owner_filter = None if user["role"] == "admin" else user["id"]
    members = database.list_project_members(project_id)
    member_ids = {member["id"] for member in members}
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="project.html",
        context=web.page_context(
            request,
            project=project,
            devices=database.list_devices(owner_filter, project_id),
            members=members,
            available_users=[
                candidate
                for candidate in database.list_users()
                if candidate["id"] not in member_ids
                and candidate["role"] == "member"
            ],
            tasks=database.list_tasks(project_id, include_archived=True),
            sessions=database.list_work_sessions(
                None if user["role"] == "admin" else user["id"], project_id
            ),
        ),
    )


@router.post("/{project_id}/tasks")
def create_task(
    request: Request,
    project_id: str,
    name: Annotated[str, Form(min_length=1, max_length=120)],
    description: Annotated[str, Form(max_length=500)],
    billable: Annotated[int, Form(ge=0, le=1)],
    csrf: Annotated[str, Form()],
):
    web = request.app.state.web
    database = request.app.state.database
    admin = web.require_admin(request)
    web.require_csrf(request, csrf)
    if not database.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        task = database.create_task(
            project_id,
            name,
            description,
            admin["id"],
            billable=bool(billable),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    database.add_audit_event(
        admin["id"], "task.created", "task", task["id"], project_id
    )
    return RedirectResponse(
        f"/projects/{project_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/{project_id}/members")
def add_project_member(
    request: Request,
    project_id: str,
    user_id: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
):
    web = request.app.state.web
    database = request.app.state.database
    admin = web.require_admin(request)
    web.require_csrf(request, csrf)
    known_user_ids = {user["id"] for user in database.list_users()}
    if not database.get_project(project_id) or user_id not in known_user_ids:
        raise HTTPException(status_code=404, detail="Project or user not found")
    database.add_project_member(project_id, user_id)
    database.add_audit_event(
        admin["id"], "project.member_added", "project", project_id, user_id
    )
    return RedirectResponse(
        f"/projects/{project_id}", status_code=status.HTTP_303_SEE_OTHER
    )

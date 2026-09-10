from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter(prefix="/projects", tags=["projects"])


def _require_project_manager(request: Request, project_id: str) -> dict:
    user = request.app.state.web.require_user(request)
    if not request.app.state.web.can_manage_project(user, project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return user


def _require_project_member_manager(request: Request, project_id: str) -> dict:
    user = request.app.state.web.require_user(request)
    if not request.app.state.web.can_manage_project_members(user, project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return user


def _can_manage_member_target(
    request: Request, actor: dict, project_id: str, target_user_id: str
) -> bool:
    database = request.app.state.database
    return (
        actor["role"] in {"admin", "manager"}
        or database.project_member_role(project_id, actor["id"]) == "manager"
        or database.team_lead_can_manage_user(
            actor["id"], target_user_id, "manage_members"
        )
    )


@router.post("")
def create_project(
    request: Request,
    name: Annotated[str, Form(min_length=1, max_length=100)],
    csrf: Annotated[str, Form()],
    description: Annotated[str, Form(max_length=500)] = "",
    team_id: Annotated[str, Form()] = "",
):
    web = request.app.state.web
    database = request.app.state.database
    actor = web.require_user(request)
    web.require_csrf(request, csrf)
    elevated = actor["role"] in {"admin", "manager"}
    if not elevated and (
        not team_id
        or not database.team_lead_can_manage_team(
            actor["id"], team_id, "manage_projects"
        )
    ):
        raise HTTPException(status_code=403, detail="Project creation is not delegated")
    if team_id and not database.get_team(team_id):
        raise HTTPException(status_code=404, detail="Team not found")
    try:
        project = database.create_project(name, description, actor["id"])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if elevated:
        database.add_project_member(project["id"], actor["id"])
    if team_id:
        database.add_team_project(team_id, project["id"])
    database.add_audit_event(
        actor["id"], "project.created", "project", project["id"], project["name"]
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
    can_manage_project = web.can_manage_project(user, project_id)
    can_manage_members = web.can_manage_project_members(user, project_id)
    can_view_team_data = web.can_view_project_team_data(user, project_id)
    owner_filter = None if can_view_team_data else user["id"]
    members = database.list_project_members(project_id)
    member_ids = {member["id"] for member in members}
    available_scope = None
    if (
        user["role"] not in {"admin", "manager"}
        and database.project_member_role(project_id, user["id"]) != "manager"
    ):
        available_scope = {
            member["id"]
            for member in database.team_lead_members(user["id"], "manage_members")
        }
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
                and candidate["role"] in {"member", "viewer"}
                and (available_scope is None or candidate["id"] in available_scope)
            ],
            tasks=database.list_tasks(
                project_id,
                include_archived=True,
                user_id=None if can_view_team_data else user["id"],
            ),
            sessions=database.list_work_sessions(
                None if can_view_team_data else user["id"],
                project_id,
            ),
            clients=database.finance_summary(include_payroll=False)["clients"]
            if can_manage_project
            else [],
            todos=database.list_project_todos(project_id),
            can_track_project=web.can_track_project(user, project_id),
            can_manage_project=can_manage_project,
            can_manage_members=can_manage_members,
            can_edit_project_work=web.can_track_project(user, project_id),
        ),
    )


@router.post("/{project_id}/todos")
def create_project_todo(
    request: Request,
    project_id: str,
    name: Annotated[str, Form(min_length=1, max_length=160)],
    description: Annotated[str, Form(max_length=500)] = "",
    add_to_future_projects: Annotated[int, Form(ge=0, le=1)] = 0,
    csrf: Annotated[str, Form()] = "",
):
    web = request.app.state.web
    user = web.require_worker(request)
    web.require_csrf(request, csrf)
    if not web.can_track_project(user, project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    if add_to_future_projects and user["role"] not in {"admin", "manager"}:
        raise HTTPException(
            status_code=403,
            detail="Only organization administrators can add work to future projects",
        )
    request.app.state.database.create_global_todo(
        name, description, [project_id], bool(add_to_future_projects), user["id"]
    )
    return RedirectResponse(
        f"/projects/{project_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/{project_id}/todos/{todo_id}/complete")
def complete_project_todo(
    request: Request,
    project_id: str,
    todo_id: str,
    complete: Annotated[int, Form(ge=0, le=1)],
    csrf: Annotated[str, Form()],
):
    web = request.app.state.web
    user = web.require_worker(request)
    web.require_csrf(request, csrf)
    if not web.can_track_project(user, project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    request.app.state.database.set_todo_complete(
        todo_id, project_id, user["id"], bool(complete)
    )
    return RedirectResponse(
        f"/projects/{project_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/{project_id}/settings")
def update_project_settings(
    request: Request,
    project_id: str,
    color: Annotated[str, Form()],
    budget_type: Annotated[str, Form()],
    budget_amount: Annotated[str, Form()],
    budget_minutes: Annotated[int, Form(ge=0)],
    billable_rate: Annotated[str, Form()],
    client_id: Annotated[str, Form()] = "",
    csrf: Annotated[str, Form()] = "",
):
    web, database = request.app.state.web, request.app.state.database
    admin = _require_project_manager(request, project_id)
    web.require_csrf(request, csrf)
    try:
        database.update_project_finances(
            project_id,
            color,
            budget_type,
            Decimal(budget_amount),
            budget_minutes,
            Decimal(billable_rate),
            client_id or None,
        )
    except (ValueError, InvalidOperation) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    database.add_audit_event(
        admin["id"], "project.settings_updated", "project", project_id, budget_type
    )
    return RedirectResponse(
        f"/projects/{project_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/{project_id}/tasks")
def create_task(
    request: Request,
    project_id: str,
    name: Annotated[str, Form(min_length=1, max_length=120)],
    csrf: Annotated[str, Form()],
    description: Annotated[str, Form(max_length=500)] = "",
    billable: Annotated[int, Form(ge=0, le=1)] = 1,
):
    web = request.app.state.web
    database = request.app.state.database
    admin = _require_project_manager(request, project_id)
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
    project_role: Annotated[str, Form()] = "worker",
):
    web = request.app.state.web
    database = request.app.state.database
    admin = _require_project_member_manager(request, project_id)
    web.require_csrf(request, csrf)
    known_user_ids = {
        user["id"]
        for user in database.list_users()
        if user["role"] in {"member", "viewer"}
    }
    if (
        not database.get_project(project_id)
        or user_id not in known_user_ids
        or database.is_project_member(project_id, user_id)
        or not _can_manage_member_target(request, admin, project_id, user_id)
    ):
        raise HTTPException(status_code=404, detail="Project or user not found")
    try:
        database.add_project_member(project_id, user_id, project_role)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    database.add_audit_event(
        admin["id"],
        "project.member_added",
        "project",
        project_id,
        f"{user_id}:{project_role}",
    )
    return RedirectResponse(
        f"/projects/{project_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/{project_id}/members/{user_id}/remove")
def remove_project_member(
    request: Request, project_id: str, user_id: str, csrf: Annotated[str, Form()]
):
    web = request.app.state.web
    admin = _require_project_member_manager(request, project_id)
    web.require_csrf(request, csrf)
    if admin["id"] == user_id and admin["role"] == "member":
        raise HTTPException(
            status_code=422, detail="A project manager cannot remove themselves"
        )
    if not _can_manage_member_target(request, admin, project_id, user_id):
        raise HTTPException(status_code=404, detail="Project member not found")
    request.app.state.database.remove_project_member(project_id, user_id)
    request.app.state.database.add_audit_event(
        admin["id"], "project.member_removed", "project", project_id, user_id
    )
    return RedirectResponse(
        f"/projects/{project_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/{project_id}/members/{user_id}/role")
def change_project_member_role(
    request: Request,
    project_id: str,
    user_id: str,
    project_role: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
):
    web = request.app.state.web
    actor = _require_project_member_manager(request, project_id)
    web.require_csrf(request, csrf)
    if actor["id"] == user_id and actor["role"] == "member":
        raise HTTPException(
            status_code=422, detail="A project manager cannot change their own role"
        )
    if not _can_manage_member_target(request, actor, project_id, user_id):
        raise HTTPException(status_code=404, detail="Project member not found")
    try:
        request.app.state.database.set_project_member_role(
            project_id, user_id, project_role
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        actor["id"],
        "project.member_role_changed",
        "project",
        project_id,
        f"{user_id}:{project_role}",
    )
    return RedirectResponse(
        f"/projects/{project_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/{project_id}/tasks/{task_id}/status")
def change_task_status(
    request: Request,
    project_id: str,
    task_id: str,
    task_status: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
):
    web = request.app.state.web
    admin = _require_project_manager(request, project_id)
    web.require_csrf(request, csrf)
    if task_id not in {
        task["id"]
        for task in request.app.state.database.list_tasks(
            project_id, include_archived=True
        )
    }:
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        request.app.state.database.set_task_status(task_id, task_status)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    request.app.state.database.add_audit_event(
        admin["id"], "task.status_changed", "task", task_id, task_status
    )
    return RedirectResponse(
        f"/projects/{project_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/{project_id}/enabled")
def change_project_state(
    request: Request,
    project_id: str,
    enabled: Annotated[int, Form(ge=0, le=1)],
    csrf: Annotated[str, Form()],
):
    web = request.app.state.web
    admin = web.require_admin(request)
    web.require_csrf(request, csrf)
    try:
        request.app.state.database.set_project_enabled(project_id, bool(enabled))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc
    request.app.state.database.add_audit_event(
        admin["id"],
        "project.enabled_changed",
        "project",
        project_id,
        str(bool(enabled)),
    )
    return RedirectResponse(
        f"/projects/{project_id}", status_code=status.HTTP_303_SEE_OTHER
    )

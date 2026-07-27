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
    owner_filter = None if user["role"] == "admin" else user["id"]
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=web.page_context(
            request,
            devices=database.list_devices(owner_filter),
            users=database.list_users() if user["role"] == "admin" else [],
            projects=database.list_projects(
                None if user["role"] == "admin" else user["id"]
            ),
        ),
    )

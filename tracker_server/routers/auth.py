from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from ..security import hash_password, verify_password
from ..web import normalize_email


router = APIRouter(tags=["authentication"])


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    web = request.app.state.web
    if web.current_user(request):
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="login.html",
        context=web.page_context(request, error=None),
    )


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
):
    web = request.app.state.web
    database = request.app.state.database
    web.require_csrf(request, csrf)
    try:
        normalized = normalize_email(email)
    except ValueError:
        normalized = "invalid@example.invalid"
    user = database.get_user_by_email(normalized)
    valid = verify_password(
        password,
        user["password_hash"] if user else request.app.state.dummy_password_hash,
    )
    if not user or not valid:
        return request.app.state.templates.TemplateResponse(
            request=request,
            name="login.html",
            context=web.page_context(request, error="Incorrect email or password"),
            status_code=401,
        )
    request.session.clear()
    request.session["user_id"] = user["id"]
    request.session["csrf_token"] = secrets.token_urlsafe(24)
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
def logout(request: Request, csrf: Annotated[str, Form()]):
    web = request.app.state.web
    web.require_user(request)
    web.require_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@router.get(
    "/invite/{token}", response_class=HTMLResponse, name="accept_invite_page"
)
def accept_invite_page(request: Request, token: str):
    invitation = request.app.state.database.get_invitation(token)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="accept_invite.html",
        context=request.app.state.web.page_context(
            request, invitation=invitation, token=token, error=None
        ),
        status_code=200 if invitation else 410,
    )


@router.post("/invite/{token}", response_class=HTMLResponse)
def accept_invite(
    request: Request,
    token: str,
    password: Annotated[str, Form()],
    password_confirm: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
):
    web = request.app.state.web
    database = request.app.state.database
    web.require_csrf(request, csrf)
    invitation = database.get_invitation(token)
    error = None
    if not invitation:
        error = "This invitation is invalid, expired, or already used."
    elif password != password_confirm:
        error = "Passwords do not match."
    elif len(password) < 12:
        error = "Password must be at least 12 characters."
    if error:
        return request.app.state.templates.TemplateResponse(
            request=request,
            name="accept_invite.html",
            context=web.page_context(
                request, invitation=invitation, token=token, error=error
            ),
            status_code=400,
        )
    user = database.accept_invitation(token, hash_password(password))
    if not user:
        raise HTTPException(status_code=410, detail="Invitation is no longer valid")
    request.session.clear()
    request.session["user_id"] = user["id"]
    request.session["csrf_token"] = secrets.token_urlsafe(24)
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/invitations", response_class=HTMLResponse)
def create_invitation(
    request: Request,
    email: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
):
    web = request.app.state.web
    database = request.app.state.database
    admin = web.require_admin(request)
    web.require_csrf(request, csrf)
    try:
        normalized = normalize_email(email)
        invitation, raw_token = database.create_invitation(
            normalized, admin["id"], request.app.state.settings.invitation_hours
        )
    except ValueError as exc:
        return request.app.state.templates.TemplateResponse(
            request=request,
            name="invitation_created.html",
            context=web.page_context(
                request, error=str(exc), invitation=None
            ),
            status_code=400,
        )
    invitation["url"] = str(
        request.url_for("accept_invite_page", token=raw_token)
    )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="invitation_created.html",
        context=web.page_context(request, error=None, invitation=invitation),
    )

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from ..security import generate_totp_secret, hash_password, verify_password, verify_totp
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
        # Failed sign-ins are recorded without an actor; the attempted address is
        # the only detail, so a brute-force or credential-stuffing attempt is visible.
        database.add_audit_event(
            user["id"] if user else None, "auth.login_failed", "user", None, normalized
        )
        return request.app.state.templates.TemplateResponse(
            request=request,
            name="login.html",
            context=web.page_context(request, error="Incorrect email or password"),
            status_code=401,
        )
    request.session.clear()
    request.session["csrf_token"] = secrets.token_urlsafe(24)
    policy = database.organization_settings()
    if user.get("two_factor_enabled") or policy["require_two_factor"]:
        request.session["preauth_user_id"] = user["id"]
        return RedirectResponse(
            "/two-factor" if user.get("totp_secret") else "/two-factor/setup",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    request.session["user_id"] = user["id"]
    database.add_audit_event(
        user["id"], "auth.login", "user", user["id"], user["email"]
    )
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


def _preauth_user(request: Request):
    user_id = request.session.get("preauth_user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Sign in with your password first")
    user = request.app.state.database.get_user(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Account is unavailable")
    return user


@router.get("/two-factor", response_class=HTMLResponse)
def two_factor_page(request: Request):
    _preauth_user(request)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="two_factor.html",
        context=request.app.state.web.page_context(request, error=None),
    )


@router.post("/two-factor", response_class=HTMLResponse)
def two_factor_verify(
    request: Request,
    code: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
):
    user = _preauth_user(request)
    request.app.state.web.require_csrf(request, csrf)
    if not verify_totp(user.get("totp_secret") or "", code.strip()):
        request.app.state.database.add_audit_event(
            user["id"], "auth.two_factor_failed", "user", user["id"]
        )
        return request.app.state.templates.TemplateResponse(
            request=request,
            name="two_factor.html",
            context=request.app.state.web.page_context(
                request, error="The authentication code is invalid or expired."
            ),
            status_code=401,
        )
    request.session.pop("preauth_user_id", None)
    request.session["user_id"] = user["id"]
    request.app.state.database.add_audit_event(
        user["id"], "auth.two_factor", "user", user["id"]
    )
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/two-factor/setup", response_class=HTMLResponse)
def two_factor_setup_page(request: Request):
    user = _preauth_user(request)
    secret = user.get("totp_secret") or generate_totp_secret()
    if not user.get("totp_secret"):
        request.app.state.database.set_two_factor_secret(
            user["id"], secret, enabled=False
        )
    uri = f"otpauth://totp/Dayfinch:{user['email']}?secret={secret}&issuer=Dayfinch"
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="two_factor_setup.html",
        context=request.app.state.web.page_context(
            request, secret=secret, provisioning_uri=uri, error=None
        ),
    )


@router.post("/two-factor/setup", response_class=HTMLResponse)
def two_factor_setup(
    request: Request,
    code: Annotated[str, Form()],
    csrf: Annotated[str, Form()],
):
    user = _preauth_user(request)
    request.app.state.web.require_csrf(request, csrf)
    if not verify_totp(user.get("totp_secret") or "", code.strip()):
        uri = f"otpauth://totp/Dayfinch:{user['email']}?secret={user.get('totp_secret', '')}&issuer=Dayfinch"
        return request.app.state.templates.TemplateResponse(
            request=request,
            name="two_factor_setup.html",
            context=request.app.state.web.page_context(
                request,
                secret=user.get("totp_secret", ""),
                provisioning_uri=uri,
                error="Enter the current six-digit code to finish setup.",
            ),
            status_code=400,
        )
    request.app.state.database.set_two_factor_secret(
        user["id"], user["totp_secret"], enabled=True
    )
    request.session.pop("preauth_user_id", None)
    request.session["user_id"] = user["id"]
    request.app.state.database.add_audit_event(
        user["id"], "auth.two_factor_enabled", "user", user["id"]
    )
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
def logout(request: Request, csrf: Annotated[str, Form()]):
    web = request.app.state.web
    user = web.require_user(request)
    web.require_csrf(request, csrf)
    request.app.state.database.add_audit_event(
        user["id"], "auth.logout", "user", user["id"], user["email"]
    )
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/invite/{token}", response_class=HTMLResponse, name="accept_invite_page")
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
    database.add_audit_event(
        user["id"], "invitation.accepted", "user", user["id"], user["email"]
    )
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
            context=web.page_context(request, error=str(exc), invitation=None),
            status_code=400,
        )
    database.add_audit_event(
        admin["id"], "invitation.created", "invitation", invitation["id"], normalized
    )
    invitation["url"] = str(request.url_for("accept_invite_page", token=raw_token))
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="invitation_created.html",
        context=web.page_context(request, error=None, invitation=invitation),
    )

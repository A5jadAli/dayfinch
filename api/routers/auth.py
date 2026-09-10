from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..security import generate_totp_secret, hash_password, verify_password, verify_totp
from ..services.invitation_delivery import InvitationDeliveryError
from ..services.oidc import OIDCAuthenticationError
from ..services.saml import SAMLAuthenticationError
from ..web import normalize_email

router = APIRouter(tags=["authentication"])


def _sso_context(request: Request) -> dict[str, str | bool]:
    policy = request.app.state.database.organization_settings()
    provider = policy["sso_provider"]
    if provider == "OpenID Connect" and request.app.state.oidc.enabled:
        return {
            "sso_enabled": True,
            "sso_url": "/auth/oidc",
            "sso_label": "Continue with company SSO",
        }
    if provider == "SAML 2.0" and request.app.state.saml.enabled:
        return {
            "sso_enabled": True,
            "sso_url": "/auth/saml",
            "sso_label": "Continue with company SSO",
        }
    return {"sso_enabled": False, "sso_url": "", "sso_label": ""}


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    web = request.app.state.web
    if web.current_user(request):
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="login.html",
        context=web.page_context(
            request,
            error=None,
            **_sso_context(request),
        ),
    )


def _oidc_is_enabled(request: Request) -> bool:
    policy = request.app.state.database.organization_settings()
    return bool(
        request.app.state.oidc.enabled and policy["sso_provider"] == "OpenID Connect"
    )


def _saml_is_enabled(request: Request) -> bool:
    policy = request.app.state.database.organization_settings()
    return bool(request.app.state.saml.enabled and policy["sso_provider"] == "SAML 2.0")


def _login_error(request: Request, message: str, status_code: int = 401):
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="login.html",
        context=request.app.state.web.page_context(
            request,
            error=message,
            **_sso_context(request),
        ),
        status_code=status_code,
    )


def _sso_user(request: Request, identity: dict) -> tuple[dict, dict]:
    database = request.app.state.database
    policy = database.organization_settings()
    required_domain = str(policy["sso_domain"]).strip().lower().lstrip("@")
    try:
        email = normalize_email(str(identity.get("email", "")))
    except ValueError as exc:
        raise ValueError("The identity provider returned an invalid email") from exc
    issuer = str(identity.get("issuer", ""))
    subject = str(identity.get("subject", ""))
    if not issuer or len(issuer) > 1024 or not subject or len(subject) > 512:
        raise ValueError("The identity provider returned an invalid subject")
    email_domain = email.rsplit("@", 1)[1]
    if not required_domain or email_domain != required_domain:
        raise ValueError("Your verified email is outside this workspace's SSO domain")
    user = database.get_user_by_sso_identity(issuer, subject)
    if not user:
        user = database.get_user_by_email(email)
        if not user:
            raise ValueError(
                "No active Dayfinch account matches this identity; request an invitation"
            )
        database.link_sso_identity(user["id"], issuer, subject)
    return user, policy


def _finish_sso_login(request: Request, user: dict, policy: dict):
    database = request.app.state.database
    request.session.clear()
    request.session["csrf_token"] = secrets.token_urlsafe(24)
    if user.get("two_factor_enabled") or policy["require_two_factor"]:
        request.session["preauth_user_id"] = user["id"]
        return RedirectResponse(
            "/two-factor" if user.get("totp_secret") else "/two-factor/setup",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    request.session["user_id"] = user["id"]
    database.add_audit_event(
        user["id"], "auth.sso_login", "user", user["id"], user["email"]
    )
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/auth/oidc")
async def oidc_login(request: Request):
    if not _oidc_is_enabled(request):
        raise HTTPException(status_code=404, detail="Single sign-on is not enabled")
    try:
        return await request.app.state.oidc.begin(request)
    except OIDCAuthenticationError as exc:
        return _login_error(request, str(exc), status_code=503)


@router.get("/auth/oidc/callback")
async def oidc_callback(request: Request):
    if not _oidc_is_enabled(request):
        raise HTTPException(status_code=404, detail="Single sign-on is not enabled")
    database = request.app.state.database
    try:
        identity = await request.app.state.oidc.authenticate(request)
        user, policy = _sso_user(request, identity)
    except (OIDCAuthenticationError, ValueError) as exc:
        database.add_audit_event(None, "auth.sso_failed", "user", None, str(exc))
        return _login_error(request, str(exc))

    return _finish_sso_login(request, user, policy)


@router.get("/auth/saml")
def saml_login(request: Request):
    if not _saml_is_enabled(request):
        raise HTTPException(status_code=404, detail="Single sign-on is not enabled")
    try:
        location = request.app.state.saml.begin(request)
    except SAMLAuthenticationError as exc:
        return _login_error(request, str(exc), status_code=503)
    return RedirectResponse(location, status_code=status.HTTP_302_FOUND)


@router.post("/auth/saml/acs")
async def saml_callback(request: Request):
    if not _saml_is_enabled(request):
        raise HTTPException(status_code=404, detail="Single sign-on is not enabled")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(
            status_code=415,
            detail="SAML responses must use form URL encoding",
        )
    content_length = request.headers.get("content-length", "")
    if content_length:
        try:
            length = int(content_length)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid Content-Length"
            ) from exc
        if length < 0 or length > 2_000_000:
            raise HTTPException(status_code=413, detail="SAML response is too large")
    database = request.app.state.database
    try:
        form = await request.form(max_files=0, max_fields=10, max_part_size=1_600_000)
        identity = request.app.state.saml.authenticate(
            request,
            str(form.get("SAMLResponse", "")),
            str(form.get("RelayState", "")),
        )
        if not database.consume_saml_assertion(
            identity["response_id"], identity["assertion_id"], identity["expires_at"]
        ):
            raise SAMLAuthenticationError("This SAML assertion was already used")
        user, policy = _sso_user(request, identity)
    except (SAMLAuthenticationError, ValueError) as exc:
        database.add_audit_event(None, "auth.sso_failed", "user", None, str(exc))
        return _login_error(request, str(exc))
    return _finish_sso_login(request, user, policy)


@router.get("/auth/saml/metadata")
def saml_metadata(request: Request):
    if not request.app.state.saml.enabled:
        raise HTTPException(status_code=404, detail="SAML is not configured")
    try:
        metadata = request.app.state.saml.metadata()
    except SAMLAuthenticationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(
        metadata,
        media_type="application/samlmetadata+xml",
        headers={"Cache-Control": "no-store"},
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
    source = request.client.host if request.client else "unknown"
    identity_key, source_key = web.login_throttle_keys(normalized, source)
    settings = request.app.state.settings
    if database.login_is_rate_limited(
        identity_key,
        source_key,
        identity_limit=settings.login_identity_failure_limit,
        source_limit=settings.login_source_failure_limit,
        window_minutes=settings.login_window_minutes,
    ):
        database.add_audit_event(None, "auth.login_throttled", "user", None, normalized)
        response = _login_error(
            request, "Too many sign-in attempts. Try again later.", status_code=429
        )
        response.headers["Retry-After"] = str(settings.login_window_minutes * 60)
        return response
    user = database.get_user_by_email(normalized)
    stored_password_hash = user.get("password_hash") if user else None
    valid = verify_password(
        password,
        stored_password_hash or request.app.state.dummy_password_hash,
    )
    if not user or not valid:
        database.record_login_failure(identity_key, source_key)
        # Failed sign-ins are recorded without an actor; the attempted address is
        # the only detail, so a brute-force or credential-stuffing attempt is visible.
        database.add_audit_event(
            user["id"] if user else None, "auth.login_failed", "user", None, normalized
        )
        return _login_error(request, "Incorrect email or password")
    database.clear_login_failures(identity_key)
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


def _factor_throttle(request: Request, user: dict) -> tuple[str, str, bool]:
    source = request.client.host if request.client else "unknown"
    identity_key, source_key = request.app.state.web.login_throttle_keys(
        f"two-factor:{user['id']}", source
    )
    settings = request.app.state.settings
    limited = request.app.state.database.login_is_rate_limited(
        identity_key,
        source_key,
        identity_limit=settings.login_identity_failure_limit,
        source_limit=settings.login_source_failure_limit,
        window_minutes=settings.login_window_minutes,
    )
    return identity_key, source_key, limited


def _factor_rate_limited_response(request: Request, template: str, **context):
    settings = request.app.state.settings
    response = request.app.state.templates.TemplateResponse(
        request=request,
        name=template,
        context=request.app.state.web.page_context(
            request,
            error="Too many authentication-code attempts. Try again later.",
            **context,
        ),
        status_code=429,
    )
    response.headers["Retry-After"] = str(settings.login_window_minutes * 60)
    return response


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
    identity_key, source_key, limited = _factor_throttle(request, user)
    if limited:
        request.app.state.database.add_audit_event(
            user["id"], "auth.two_factor_throttled", "user", user["id"]
        )
        return _factor_rate_limited_response(request, "two_factor.html")
    if not verify_totp(user.get("totp_secret") or "", code.strip()):
        request.app.state.database.record_login_failure(identity_key, source_key)
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
    request.app.state.database.clear_login_failures(identity_key)
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
    identity_key, source_key, limited = _factor_throttle(request, user)
    uri = f"otpauth://totp/Dayfinch:{user['email']}?secret={user.get('totp_secret', '')}&issuer=Dayfinch"
    if limited:
        request.app.state.database.add_audit_event(
            user["id"], "auth.two_factor_setup_throttled", "user", user["id"]
        )
        return _factor_rate_limited_response(
            request,
            "two_factor_setup.html",
            secret=user.get("totp_secret", ""),
            provisioning_uri=uri,
        )
    if not verify_totp(user.get("totp_secret") or "", code.strip()):
        request.app.state.database.record_login_failure(identity_key, source_key)
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
    request.app.state.database.clear_login_failures(identity_key)
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
    logout_url = (
        request.app.state.oidc.logout_url()
        if user.get("sso_issuer") == request.app.state.settings.oidc_issuer
        and _oidc_is_enabled(request)
        else ""
    )
    request.session.clear()
    return RedirectResponse(
        logout_url or "/login", status_code=status.HTTP_303_SEE_OTHER
    )


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
    invitation["url"] = f"{request.app.state.settings.public_url}/invite/{raw_token}"
    try:
        email_sent = request.app.state.invitation_delivery.deliver(
            invitation["email"], invitation["url"], str(invitation["expires_at"])
        )
        delivery_error = None
    except InvitationDeliveryError:
        email_sent = False
        delivery_error = (
            "Email delivery failed. Copy the link below and check the SMTP settings."
        )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="invitation_created.html",
        context=web.page_context(
            request,
            error=None,
            invitation=invitation,
            email_sent=email_sent,
            delivery_error=delivery_error,
        ),
    )

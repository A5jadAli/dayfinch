from __future__ import annotations

import asyncio
import hmac
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from .config import Settings
from .database import Database
from .security import hash_password, verify_password
from .storage import create_storage


PACKAGE_DIR = Path(__file__).resolve().parent


class Heartbeat(BaseModel):
    platform: str = Field(min_length=1, max_length=80)
    status: str = Field(default="active", min_length=1, max_length=40)


def normalize_email(email: str) -> str:
    normalized = email.strip().lower()
    if (
        len(normalized) > 254
        or normalized.count("@") != 1
        or " " in normalized
        or normalized.startswith("@")
        or normalized.endswith("@")
    ):
        raise ValueError("Enter a valid email address")
    return normalized


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.prepare()
    database = Database(settings.database_path)
    storage = create_storage(settings)
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    dummy_password_hash = hash_password("invalid-password-for-timing-only")

    def purge_expired() -> None:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=settings.retention_days)
        ).isoformat()
        while records := database.records_before(cutoff):
            for record in records:
                storage.delete(
                    record["screenshot_path"], record.get("storage_version_id")
                )
                database.delete_record(record["id"])

    async def retention_worker() -> None:
        while True:
            await asyncio.sleep(60 * 60)
            await asyncio.to_thread(purge_expired)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        database.initialize()
        database.bootstrap_admin(
            settings.admin_email, hash_password(settings.admin_password)
        )
        purge_expired()
        task = asyncio.create_task(retention_worker())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title="Dayfinch", version="0.2.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.database = database
    app.state.storage = storage
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="tracker_session",
        same_site="strict",
        https_only=settings.cookie_secure,
        max_age=8 * 60 * 60,
    )
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    def csrf_token(request: Request) -> str:
        token = request.session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(24)
            request.session["csrf_token"] = token
        return token

    def require_csrf(request: Request, supplied: str) -> None:
        expected = request.session.get("csrf_token", "")
        if not expected or not hmac.compare_digest(expected, supplied):
            raise HTTPException(status_code=403, detail="Invalid form token")

    def current_user(request: Request) -> dict[str, Any] | None:
        user_id = request.session.get("user_id")
        return database.get_user(user_id) if user_id else None

    def page_context(request: Request, **values: Any) -> dict[str, Any]:
        return {
            "request": request,
            "csrf_token": csrf_token(request),
            "current_user": current_user(request),
            **values,
        }

    def require_user(request: Request) -> dict[str, Any]:
        user = current_user(request)
        if not user:
            raise HTTPException(status_code=401, detail="Login required")
        return user

    def require_admin(request: Request) -> dict[str, Any]:
        user = require_user(request)
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="Administrator access required")
        return user

    def user_or_login(request: Request) -> RedirectResponse | None:
        if not current_user(request):
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        return None

    def can_access_device(user: dict[str, Any], device: dict[str, Any]) -> bool:
        return user["role"] == "admin" or device.get("owner_user_id") == user["id"]

    def can_access_record(user: dict[str, Any], record: dict[str, Any]) -> bool:
        return user["role"] == "admin" or record.get("owner_user_id") == user["id"]

    def device_from_authorization(
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="Missing device bearer token")
        device = database.authenticate_device(token)
        if not device:
            raise HTTPException(status_code=401, detail="Invalid or revoked device token")
        return device

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        if current_user(request):
            return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context=page_context(request, error=None),
        )

    @app.post("/login", response_class=HTMLResponse)
    def login(
        request: Request,
        email: Annotated[str, Form()],
        password: Annotated[str, Form()],
        csrf: Annotated[str, Form()],
    ):
        require_csrf(request, csrf)
        try:
            normalized = normalize_email(email)
        except ValueError:
            normalized = "invalid@example.invalid"
        user = database.get_user_by_email(normalized)
        valid = verify_password(password, user["password_hash"] if user else dummy_password_hash)
        if not user or not valid:
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context=page_context(request, error="Incorrect email or password"),
                status_code=401,
            )
        request.session.clear()
        request.session["user_id"] = user["id"]
        request.session["csrf_token"] = secrets.token_urlsafe(24)
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/logout")
    def logout(request: Request, csrf: Annotated[str, Form()]):
        require_user(request)
        require_csrf(request, csrf)
        request.session.clear()
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/invite/{token}", response_class=HTMLResponse, name="accept_invite_page")
    def accept_invite_page(request: Request, token: str):
        invitation = database.get_invitation(token)
        return templates.TemplateResponse(
            request=request,
            name="accept_invite.html",
            context=page_context(
                request,
                invitation=invitation,
                token=token,
                error=None,
            ),
            status_code=200 if invitation else 410,
        )

    @app.post("/invite/{token}", response_class=HTMLResponse)
    def accept_invite(
        request: Request,
        token: str,
        password: Annotated[str, Form()],
        password_confirm: Annotated[str, Form()],
        csrf: Annotated[str, Form()],
    ):
        require_csrf(request, csrf)
        invitation = database.get_invitation(token)
        error = None
        if not invitation:
            error = "This invitation is invalid, expired, or already used."
        elif password != password_confirm:
            error = "Passwords do not match."
        elif len(password) < 12:
            error = "Password must be at least 12 characters."
        if error:
            return templates.TemplateResponse(
                request=request,
                name="accept_invite.html",
                context=page_context(
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

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        redirect = user_or_login(request)
        if redirect:
            return redirect
        user = require_user(request)
        owner_filter = None if user["role"] == "admin" else user["id"]
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=page_context(
                request,
                devices=database.list_devices(owner_filter),
                users=database.list_users() if user["role"] == "admin" else [],
            ),
        )

    @app.post("/invitations", response_class=HTMLResponse)
    def create_invitation(
        request: Request,
        email: Annotated[str, Form()],
        csrf: Annotated[str, Form()],
    ):
        admin = require_admin(request)
        require_csrf(request, csrf)
        try:
            normalized = normalize_email(email)
            invitation, raw_token = database.create_invitation(
                normalized, admin["id"], settings.invitation_hours
            )
        except ValueError as exc:
            return templates.TemplateResponse(
                request=request,
                name="invitation_created.html",
                context=page_context(request, error=str(exc), invitation=None),
                status_code=400,
            )
        invitation["url"] = str(
            request.url_for("accept_invite_page", token=raw_token)
        )
        return templates.TemplateResponse(
            request=request,
            name="invitation_created.html",
            context=page_context(request, error=None, invitation=invitation),
        )

    @app.post("/devices", response_class=HTMLResponse)
    def enroll_device(
        request: Request,
        name: Annotated[str, Form(min_length=1, max_length=100)],
        csrf: Annotated[str, Form()],
    ):
        user = require_user(request)
        require_csrf(request, csrf)
        device, raw_token = database.create_device(name, user["id"])
        return templates.TemplateResponse(
            request=request,
            name="enrollment.html",
            context=page_context(request, device=device, raw_token=raw_token),
        )

    @app.get("/devices/{device_id}", response_class=HTMLResponse)
    def device_timeline(request: Request, device_id: str):
        redirect = user_or_login(request)
        if redirect:
            return redirect
        user = require_user(request)
        device = database.get_device(device_id)
        if not device or not can_access_device(user, device):
            raise HTTPException(status_code=404, detail="Device not found")
        return templates.TemplateResponse(
            request=request,
            name="device.html",
            context=page_context(
                request,
                device=device,
                records=database.list_records(device_id),
            ),
        )

    @app.post("/devices/{device_id}/enabled")
    def change_device_state(
        request: Request,
        device_id: str,
        enabled: Annotated[int, Form(ge=0, le=1)],
        csrf: Annotated[str, Form()],
    ):
        user = require_user(request)
        require_csrf(request, csrf)
        device = database.get_device(device_id)
        if not device or not can_access_device(user, device):
            raise HTTPException(status_code=404, detail="Device not found")
        database.set_device_enabled(device_id, bool(enabled))
        return RedirectResponse(
            f"/devices/{device_id}", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.get("/screenshots/{record_id}")
    def screenshot(request: Request, record_id: str):
        user = require_user(request)
        record = database.get_record(record_id)
        if not record or not can_access_record(user, record):
            raise HTTPException(status_code=404, detail="Screenshot not found")
        try:
            content = storage.read(record["screenshot_path"])
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Screenshot file missing") from exc
        return Response(
            content=content.data,
            media_type=content.content_type,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/screenshots/{record_id}/delete")
    def delete_screenshot(
        request: Request,
        record_id: str,
        csrf: Annotated[str, Form()],
    ):
        user = require_user(request)
        require_csrf(request, csrf)
        record = database.get_record(record_id)
        if not record or not can_access_record(user, record):
            raise HTTPException(status_code=404, detail="Screenshot not found")
        storage.delete(record["screenshot_path"], record.get("storage_version_id"))
        database.delete_record(record_id)
        return RedirectResponse(
            f"/devices/{record['device_id']}", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.post("/api/v1/heartbeat")
    def heartbeat(
        payload: Heartbeat,
        device: dict[str, Any] = Depends(device_from_authorization),
    ) -> dict[str, str]:
        database.touch_device(device["id"], payload.platform, payload.status)
        return {"status": "ok"}

    @app.post("/api/v1/activity", status_code=201)
    async def ingest_activity(
        record_id: Annotated[str, Form()],
        captured_at: Annotated[str, Form()],
        keyboard_events: Annotated[int, Form(ge=0, le=1_000_000)],
        mouse_clicks: Annotated[int, Form(ge=0, le=1_000_000)],
        mouse_distance: Annotated[int, Form(ge=0, le=1_000_000_000)],
        active_app: Annotated[str, Form(max_length=160)],
        agent_version: Annotated[str, Form(min_length=1, max_length=40)],
        screenshot_file: Annotated[UploadFile, File()],
        device: dict[str, Any] = Depends(device_from_authorization),
    ) -> dict[str, str]:
        try:
            parsed_id = str(uuid.UUID(record_id))
            captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
            if captured.tzinfo is None:
                raise ValueError("timezone required")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid record id or timestamp") from exc

        if database.record_exists(parsed_id):
            return {"status": "duplicate", "record_id": parsed_id}

        data = await screenshot_file.read(settings.max_upload_bytes + 1)
        if not data or len(data) > settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="Screenshot is empty or too large")
        try:
            stored = storage.save(device["id"], parsed_id, captured, data)
        except ValueError as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from exc

        created = database.add_record(
            {
                "id": parsed_id,
                "device_id": device["id"],
                "captured_at": captured.isoformat(),
                "keyboard_events": keyboard_events,
                "mouse_clicks": mouse_clicks,
                "mouse_distance": mouse_distance,
                "active_app": active_app.strip()[:160] or None,
                "agent_version": agent_version,
                "screenshot_path": stored.key,
                "storage_version_id": stored.version_id,
            }
        )
        if not created:
            storage.delete(stored.key, stored.version_id)
        database.touch_device(device["id"], "", "active")
        return {"status": "created" if created else "duplicate", "record_id": parsed_id}

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("tracker_server.main:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    run()

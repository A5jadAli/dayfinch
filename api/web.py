from __future__ import annotations

import hmac
import secrets
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse

from .database import Database


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


class WebSecurity:
    """Session authentication, CSRF enforcement, and resource policies."""

    def __init__(self, database: Database):
        self.database = database

    def csrf_token(self, request: Request) -> str:
        token = request.session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(24)
            request.session["csrf_token"] = token
        return token

    def require_csrf(self, request: Request, supplied: str) -> None:
        expected = request.session.get("csrf_token", "")
        if not expected or not hmac.compare_digest(expected, supplied):
            raise HTTPException(status_code=403, detail="Invalid form token")

    def current_user(self, request: Request) -> dict[str, Any] | None:
        user_id = request.session.get("user_id")
        return self.database.get_user(user_id) if user_id else None

    def page_context(self, request: Request, **values: Any) -> dict[str, Any]:
        return {
            "request": request,
            "csrf_token": self.csrf_token(request),
            "current_user": self.current_user(request),
            **values,
        }

    def require_user(self, request: Request) -> dict[str, Any]:
        user = self.current_user(request)
        if not user:
            raise HTTPException(status_code=401, detail="Login required")
        return user

    def require_admin(self, request: Request) -> dict[str, Any]:
        user = self.require_user(request)
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="Administrator access required")
        return user

    def user_or_login(self, request: Request) -> RedirectResponse | None:
        if not self.current_user(request):
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        return None

    @staticmethod
    def can_access_device(user: dict[str, Any], device: dict[str, Any]) -> bool:
        return user["role"] == "admin" or device.get("owner_user_id") == user["id"]

    def can_access_project(self, user: dict[str, Any], project_id: str) -> bool:
        return user["role"] == "admin" or self.database.is_project_member(
            project_id, user["id"]
        )

    @staticmethod
    def can_access_record(user: dict[str, Any], record: dict[str, Any]) -> bool:
        return user["role"] == "admin" or record.get("owner_user_id") == user["id"]

    def authenticate_device(self, authorization: str | None) -> dict[str, Any]:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="Missing device bearer token")
        device = self.database.authenticate_device(token)
        if not device:
            raise HTTPException(
                status_code=401, detail="Invalid or revoked device token"
            )
        return device

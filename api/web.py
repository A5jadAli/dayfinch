from __future__ import annotations

import hmac
import secrets
from hashlib import sha256
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

    def __init__(self, database: Database, session_secret: str):
        self.database = database
        self._session_secret = session_secret.encode()

    def login_throttle_keys(self, identity: str, source: str) -> tuple[str, str]:
        def digest(value: str) -> str:
            return hmac.new(
                self._session_secret, value.encode("utf-8"), sha256
            ).hexdigest()

        return digest(f"identity:{identity}"), digest(f"source:{source}")

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
        if user["role"] not in {"admin", "manager"}:
            raise HTTPException(status_code=403, detail="Administrator access required")
        return user

    def require_owner(self, request: Request) -> dict[str, Any]:
        user = self.require_user(request)
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="Owner access required")
        return user

    def require_worker(self, request: Request) -> dict[str, Any]:
        user = self.require_user(request)
        if user["role"] == "viewer":
            raise HTTPException(
                status_code=403, detail="Read-only viewers cannot modify work"
            )
        return user

    def require_it_manager(self, request: Request) -> dict[str, Any]:
        user = self.require_user(request)
        if user["role"] not in {"admin", "manager"} and not user.get("manage_it"):
            raise HTTPException(status_code=403, detail="Manage IT access required")
        return user

    def user_or_login(self, request: Request) -> RedirectResponse | None:
        if not self.current_user(request):
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        return None

    def can_access_device(self, user: dict[str, Any], device: dict[str, Any]) -> bool:
        project_role = (
            self.database.project_member_role(device["project_id"], user["id"])
            if device.get("project_id")
            else None
        )
        return (
            user["role"] in {"admin", "manager"}
            or bool(user.get("manage_it"))
            or device.get("owner_user_id") == user["id"]
            or project_role in {"manager", "viewer"}
            or (user["role"] == "viewer" and project_role is not None)
        )

    def can_manage_device(self, user: dict[str, Any], device: dict[str, Any]) -> bool:
        return (
            user["role"] in {"admin", "manager"}
            or bool(user.get("manage_it"))
            or (user["role"] == "member" and device.get("owner_user_id") == user["id"])
        )

    def can_view_device_activity(
        self, user: dict[str, Any], device: dict[str, Any]
    ) -> bool:
        if (
            user["role"] in {"admin", "manager"}
            or device.get("owner_user_id") == user["id"]
        ):
            return True
        if not device.get("project_id"):
            return False
        project_role = self.database.project_member_role(
            device["project_id"], user["id"]
        )
        return project_role in {"manager", "viewer"} or (
            user["role"] == "viewer" and project_role is not None
        )

    def can_access_project(self, user: dict[str, Any], project_id: str) -> bool:
        return (
            user["role"] in {"admin", "manager"}
            or self.database.is_project_member(project_id, user["id"])
            or self.database.team_lead_can_manage_project(
                user["id"], project_id, "manage_projects"
            )
        )

    def can_track_project(self, user: dict[str, Any], project_id: str) -> bool:
        """Tracking is available to workers, never to read-only project viewers."""
        project = self.database.get_project(project_id)
        if not project or not project["enabled"]:
            return False
        return user["role"] in {"admin", "manager"} or (
            user["role"] == "member"
            and self.database.project_member_role(project_id, user["id"])
            in {"worker", "manager"}
        )

    def can_manage_project(self, user: dict[str, Any], project_id: str) -> bool:
        return (
            user["role"] in {"admin", "manager"}
            or (
                user["role"] == "member"
                and self.database.project_member_role(project_id, user["id"])
                == "manager"
            )
            or self.database.team_lead_can_manage_project(
                user["id"], project_id, "manage_projects"
            )
        )

    def can_manage_project_members(self, user: dict[str, Any], project_id: str) -> bool:
        return (
            user["role"] in {"admin", "manager"}
            or (
                user["role"] == "member"
                and self.database.project_member_role(project_id, user["id"])
                == "manager"
            )
            or self.database.team_lead_can_manage_project(
                user["id"], project_id, "manage_members"
            )
        )

    def can_view_project_team_data(self, user: dict[str, Any], project_id: str) -> bool:
        return (
            user["role"] in {"admin", "manager", "viewer"}
            or self.database.project_member_role(project_id, user["id"])
            in {"manager", "viewer"}
            or self.database.team_lead_can_manage_project(
                user["id"], project_id, "manage_projects"
            )
        )

    def can_access_record(self, user: dict[str, Any], record: dict[str, Any]) -> bool:
        project_role = (
            self.database.project_member_role(record["project_id"], user["id"])
            if record.get("project_id")
            else None
        )
        return (
            user["role"] in {"admin", "manager"}
            or record.get("owner_user_id") == user["id"]
            or project_role in {"manager", "viewer"}
            or (user["role"] == "viewer" and project_role is not None)
        )

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

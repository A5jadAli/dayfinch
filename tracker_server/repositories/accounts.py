from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .base import RepositoryMixin, token_hash, utc_now


class AccountsRepository(RepositoryMixin):
    def bootstrap_admin(self, email: str, password_hash: str) -> dict[str, Any]:
        normalized = email.strip().lower()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (normalized,)
            ).fetchone()
            if row and (row["role"] != "admin" or not row["enabled"]):
                raise RuntimeError("TRACKER_ADMIN_EMAIL belongs to a non-admin account")
            if not row:
                user_id = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO users(id, email, password_hash, role, enabled, created_at)
                       VALUES (?, ?, ?, 'admin', 1, ?)""",
                    (user_id, normalized, password_hash, utc_now()),
                )
                row = connection.execute(
                    "SELECT * FROM users WHERE id = ?", (user_id,)
                ).fetchone()
            else:
                connection.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (password_hash, row["id"]),
                )
                row = connection.execute(
                    "SELECT * FROM users WHERE id = ?", (row["id"],)
                ).fetchone()
        return dict(row)

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE id = ? AND enabled = 1", (user_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE email = ? COLLATE NOCASE AND enabled = 1",
                (email.strip().lower(),),
            ).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT u.*,
                          (SELECT COUNT(*) FROM devices d WHERE d.owner_user_id = u.id) device_count,
                          (SELECT COUNT(*) FROM project_members pm WHERE pm.user_id = u.id) project_count
                   FROM users u ORDER BY u.created_at DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def create_invitation(
        self, email: str, created_by_user_id: str, valid_hours: int
    ) -> tuple[dict[str, Any], str]:
        normalized = email.strip().lower()
        now = datetime.now(timezone.utc)
        raw_token = secrets.token_urlsafe(32)
        with self.connect() as connection:
            user = connection.execute(
                "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (normalized,)
            ).fetchone()
            if user and user["password_hash"] and user["enabled"]:
                raise ValueError("That email already has an active account")
            if not user:
                user_id = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO users(id, email, role, enabled, created_at)
                       VALUES (?, ?, 'member', 0, ?)""",
                    (user_id, normalized, now.isoformat()),
                )
            else:
                user_id = user["id"]
            connection.execute(
                "UPDATE invitations SET used_at = ? WHERE user_id = ? AND used_at IS NULL",
                (now.isoformat(), user_id),
            )
            invitation_id = str(uuid.uuid4())
            expires_at = (now + timedelta(hours=valid_hours)).isoformat()
            connection.execute(
                """INSERT INTO invitations(
                       id, user_id, token_hash, created_by_user_id, created_at, expires_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    invitation_id,
                    user_id,
                    token_hash(raw_token),
                    created_by_user_id,
                    now.isoformat(),
                    expires_at,
                ),
            )
        return {
            "id": invitation_id,
            "email": normalized,
            "expires_at": expires_at,
        }, raw_token

    def get_invitation(self, raw_token: str) -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """SELECT i.*, u.email FROM invitations i
                   JOIN users u ON u.id = i.user_id
                   WHERE i.token_hash = ? AND i.used_at IS NULL AND i.expires_at > ?""",
                (token_hash(raw_token), now),
            ).fetchone()
        return dict(row) if row else None

    def accept_invitation(
        self, raw_token: str, password_hash: str
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            invitation = connection.execute(
                """SELECT * FROM invitations
                   WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?""",
                (token_hash(raw_token), now),
            ).fetchone()
            if not invitation:
                return None
            connection.execute(
                "UPDATE users SET password_hash = ?, enabled = 1 WHERE id = ?",
                (password_hash, invitation["user_id"]),
            )
            connection.execute(
                "UPDATE invitations SET used_at = ? WHERE id = ?",
                (now, invitation["id"]),
            )
            user = connection.execute(
                "SELECT * FROM users WHERE id = ?", (invitation["user_id"],)
            ).fetchone()
        return dict(user)

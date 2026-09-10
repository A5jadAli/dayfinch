from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.errors import UniqueViolation

from .base import RepositoryMixin, token_hash, utc_now


class AccountsRepository(RepositoryMixin):
    def login_is_rate_limited(
        self,
        identity_hash: str,
        source_hash: str,
        *,
        identity_limit: int,
        source_limit: int,
        window_minutes: int,
    ) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT
                       COUNT(*) FILTER (WHERE identity_hash=%s) identity_failures,
                       COUNT(*) FILTER (WHERE source_hash=%s) source_failures
                     FROM login_attempts
                    WHERE attempted_at > CURRENT_TIMESTAMP - (%s * INTERVAL '1 minute')""",
                (identity_hash, source_hash, window_minutes),
            ).fetchone()
        return bool(
            row["identity_failures"] >= identity_limit
            or row["source_failures"] >= source_limit
        )

    def record_login_failure(self, identity_hash: str, source_hash: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO login_attempts(identity_hash,source_hash) VALUES (%s,%s)",
                (identity_hash, source_hash),
            )
            connection.execute(
                "DELETE FROM login_attempts WHERE attempted_at < CURRENT_TIMESTAMP - INTERVAL '24 hours'"
            )

    def clear_login_failures(self, identity_hash: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM login_attempts WHERE identity_hash=%s", (identity_hash,)
            )

    def bootstrap_admin(self, email: str, password_hash: str) -> dict[str, Any]:
        normalized = email.strip().lower()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE lower(email) = lower(%s)", (normalized,)
            ).fetchone()
            if row and (row["role"] != "admin" or not row["enabled"]):
                raise RuntimeError("TRACKER_ADMIN_EMAIL belongs to a non-admin account")
            if not row:
                user_id = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO users(id, email, password_hash, role, enabled, created_at)
                       VALUES (%s, %s, %s, 'admin', TRUE, %s)""",
                    (user_id, normalized, password_hash, utc_now()),
                )
                row = connection.execute(
                    "SELECT * FROM users WHERE id = %s", (user_id,)
                ).fetchone()
            else:
                connection.execute(
                    "UPDATE users SET password_hash = %s WHERE id = %s",
                    (password_hash, row["id"]),
                )
                row = connection.execute(
                    "SELECT * FROM users WHERE id = %s", (row["id"],)
                ).fetchone()
        return dict(row)

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE id = %s AND enabled = TRUE", (user_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_user_any(self, user_id: str) -> dict[str, Any] | None:
        """Administrative lookup that includes disabled accounts."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE id = %s", (user_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE lower(email) = lower(%s) AND enabled = TRUE",
                (email.strip().lower(),),
            ).fetchone()
        return dict(row) if row else None

    def get_user_by_sso_identity(
        self, issuer: str, subject: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM users
                   WHERE sso_issuer=%s AND sso_subject=%s AND enabled=TRUE""",
                (issuer, subject),
            ).fetchone()
        return dict(row) if row else None

    def link_sso_identity(self, user_id: str, issuer: str, subject: str) -> None:
        if not issuer or len(issuer) > 1024 or not subject or len(subject) > 512:
            raise ValueError("The identity provider returned an invalid subject")
        with self.connect() as connection:
            identity_owner = connection.execute(
                "SELECT id FROM users WHERE sso_issuer=%s AND sso_subject=%s",
                (issuer, subject),
            ).fetchone()
            if identity_owner and identity_owner["id"] != user_id:
                raise ValueError("That SSO identity is already linked to another user")
            user = connection.execute(
                "SELECT sso_issuer,sso_subject FROM users WHERE id=%s AND enabled=TRUE",
                (user_id,),
            ).fetchone()
            if not user:
                raise ValueError("The Dayfinch account is unavailable")
            if user["sso_subject"] and (
                user["sso_issuer"] != issuer or user["sso_subject"] != subject
            ):
                raise ValueError("This account is linked to a different SSO identity")
            connection.execute(
                "UPDATE users SET sso_issuer=%s,sso_subject=%s WHERE id=%s",
                (issuer, subject, user_id),
            )

    def consume_saml_assertion(
        self,
        response_id: str,
        assertion_id: str,
        expires_at: datetime,
    ) -> bool:
        if (
            not response_id
            or len(response_id) > 512
            or not assertion_id
            or len(assertion_id) > 512
        ):
            raise ValueError("SAML replay identifiers are invalid")
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM saml_assertion_replays WHERE expires_at < CURRENT_TIMESTAMP"
            )
            inserted = connection.execute(
                """INSERT INTO saml_assertion_replays(
                       response_id,assertion_id,expires_at
                   ) VALUES (%s,%s,%s)
                   ON CONFLICT DO NOTHING
                   RETURNING response_id""",
                (response_id, assertion_id, expires_at),
            ).fetchone()
        return bool(inserted)

    def list_scim_users(
        self, username: str | None, start_index: int, count: int
    ) -> tuple[list[dict[str, Any]], int]:
        where = "WHERE role IN ('member','viewer')"
        if username:
            where += " AND lower(email)=lower(%s)"
        filter_params: tuple[Any, ...] = (username,) if username else ()
        with self.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) total FROM users {where}", filter_params
            ).fetchone()["total"]
            rows = connection.execute(
                f"""SELECT * FROM users {where}
                    ORDER BY lower(email),id OFFSET %s LIMIT %s""",
                filter_params + (max(0, start_index - 1), count),
            ).fetchall()
        return [dict(row) for row in rows], int(total)

    def get_scim_user(self, user_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE id=%s AND role IN ('member','viewer')",
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def create_scim_user(
        self,
        email: str,
        external_id: str,
        display_name: str,
        enabled: bool,
    ) -> dict[str, Any]:
        user_id = str(uuid.uuid4())
        try:
            with self.connect() as connection:
                connection.execute(
                    """INSERT INTO users(
                           id,email,role,enabled,full_name,scim_external_id,
                           created_at,scim_updated_at
                       ) VALUES (%s,%s,'member',%s,%s,%s,%s,%s)""",
                    (
                        user_id,
                        email,
                        enabled,
                        display_name,
                        external_id,
                        utc_now(),
                        utc_now(),
                    ),
                )
        except UniqueViolation as exc:
            raise ValueError(
                "A user with that email or externalId already exists"
            ) from exc
        return self.get_scim_user(user_id)

    def update_scim_user(
        self,
        user_id: str,
        *,
        email: str | None = None,
        external_id: str | None = None,
        display_name: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        fields: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("email", email),
            ("scim_external_id", external_id),
            ("full_name", display_name),
            ("enabled", enabled),
        ):
            if value is not None:
                fields.append(f"{column}=%s")
                values.append(value)
        if not fields:
            user = self.get_scim_user(user_id)
            if not user:
                raise LookupError("SCIM user not found")
            return user
        fields.append("scim_updated_at=CURRENT_TIMESTAMP")
        try:
            with self.connect() as connection:
                result = connection.execute(
                    f"""UPDATE users SET {",".join(fields)}
                        WHERE id=%s AND role IN ('member','viewer')""",
                    (*values, user_id),
                )
                if result.rowcount != 1:
                    raise LookupError("SCIM user not found")
                if enabled is False:
                    now = utc_now()
                    connection.execute(
                        """UPDATE work_session_segments seg
                           SET ended_at=GREATEST(%s,seg.started_at)
                           WHERE seg.ended_at IS NULL AND EXISTS (
                             SELECT 1 FROM work_sessions ws
                             WHERE ws.id=seg.session_id AND ws.user_id=%s
                           )""",
                        (now, user_id),
                    )
                    connection.execute(
                        """UPDATE work_sessions SET
                             status='stopped',ended_at=GREATEST(%s,started_at),
                             updated_at=%s
                           WHERE user_id=%s AND status IN ('active','paused')""",
                        (now, now, user_id),
                    )
                    connection.execute(
                        """UPDATE work_breaks SET ended_at=GREATEST(%s,started_at)
                           WHERE user_id=%s AND ended_at IS NULL""",
                        (now, user_id),
                    )
                    connection.execute(
                        "UPDATE devices SET enabled=FALSE WHERE owner_user_id=%s",
                        (user_id,),
                    )
                    connection.execute(
                        """UPDATE jira_user_authorization_periods
                           SET ended_at=%s
                           WHERE user_id=%s AND ended_at IS NULL""",
                        (now, user_id),
                    )
                    connection.execute(
                        """UPDATE asana_user_authorization_periods
                           SET ended_at=%s
                           WHERE user_id=%s AND ended_at IS NULL""",
                        (now, user_id),
                    )
                    connection.execute(
                        "DELETE FROM integration_user_credentials WHERE user_id=%s",
                        (user_id,),
                    )
                    connection.execute(
                        "DELETE FROM integration_oauth_pending WHERE user_id=%s",
                        (user_id,),
                    )
                    connection.execute(
                        "DELETE FROM slack_outbox WHERE user_id=%s AND sent_at IS NULL",
                        (user_id,),
                    )
                    connection.execute(
                        """DELETE FROM jira_user_connections
                           WHERE user_id=%s AND credential_kind='member'""",
                        (user_id,),
                    )
                    connection.execute(
                        """DELETE FROM asana_user_connections
                           WHERE user_id=%s AND credential_kind='member'""",
                        (user_id,),
                    )
        except UniqueViolation as exc:
            raise ValueError(
                "A user with that email or externalId already exists"
            ) from exc
        return self.get_scim_user(user_id)

    def list_users(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT u.*,
                          (SELECT COUNT(*) FROM devices d WHERE d.owner_user_id = u.id) device_count,
                          (SELECT COUNT(*) FROM project_members pm WHERE pm.user_id = u.id) project_count
                   FROM users u ORDER BY u.created_at DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def set_two_factor_secret(
        self, user_id: str, secret: str | None, *, enabled: bool
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE users SET totp_secret=%s,two_factor_enabled=%s WHERE id=%s",
                (secret, enabled, user_id),
            )

    def set_user_enabled(self, user_id: str, enabled: bool) -> None:
        with self.connect() as connection:
            result = connection.execute(
                "UPDATE users SET enabled=%s WHERE id=%s", (enabled, user_id)
            )
            if result.rowcount != 1:
                raise ValueError("Member not found")
            if not enabled:
                now = utc_now()
                connection.execute(
                    """UPDATE work_session_segments seg
                       SET ended_at=GREATEST(%s,seg.started_at)
                       WHERE seg.ended_at IS NULL AND EXISTS (
                         SELECT 1 FROM work_sessions ws
                         WHERE ws.id=seg.session_id AND ws.user_id=%s
                       )""",
                    (now, user_id),
                )
                connection.execute(
                    """UPDATE work_sessions SET
                         status='stopped',ended_at=GREATEST(%s,started_at),updated_at=%s
                       WHERE user_id=%s AND status IN ('active','paused')""",
                    (now, now, user_id),
                )
                connection.execute(
                    """UPDATE work_breaks SET ended_at=GREATEST(%s,started_at)
                       WHERE user_id=%s AND ended_at IS NULL""",
                    (now, user_id),
                )
                connection.execute(
                    "UPDATE devices SET enabled=FALSE WHERE owner_user_id=%s",
                    (user_id,),
                )
                connection.execute(
                    """UPDATE jira_user_authorization_periods SET ended_at=%s
                       WHERE user_id=%s AND ended_at IS NULL""",
                    (now, user_id),
                )
                connection.execute(
                    """UPDATE asana_user_authorization_periods SET ended_at=%s
                       WHERE user_id=%s AND ended_at IS NULL""",
                    (now, user_id),
                )
                connection.execute(
                    "DELETE FROM integration_user_credentials WHERE user_id=%s",
                    (user_id,),
                )
                connection.execute(
                    "DELETE FROM integration_oauth_pending WHERE user_id=%s",
                    (user_id,),
                )
                connection.execute(
                    "DELETE FROM slack_outbox WHERE user_id=%s AND sent_at IS NULL",
                    (user_id,),
                )
                connection.execute(
                    """DELETE FROM jira_user_connections
                       WHERE user_id=%s AND credential_kind='member'""",
                    (user_id,),
                )
                connection.execute(
                    """DELETE FROM asana_user_connections
                       WHERE user_id=%s AND credential_kind='member'""",
                    (user_id,),
                )

    def create_invitation(
        self, email: str, created_by_user_id: str, valid_hours: int
    ) -> tuple[dict[str, Any], str]:
        normalized = email.strip().lower()
        now = datetime.now(UTC)
        raw_token = secrets.token_urlsafe(32)
        with self.connect() as connection:
            user = connection.execute(
                "SELECT * FROM users WHERE lower(email) = lower(%s)", (normalized,)
            ).fetchone()
            if user and user["password_hash"] and user["enabled"]:
                raise ValueError("That email already has an active account")
            if not user:
                user_id = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO users(id, email, role, enabled, created_at)
                       VALUES (%s, %s, 'member', FALSE, %s)""",
                    (user_id, normalized, now.isoformat()),
                )
            else:
                user_id = user["id"]
            connection.execute(
                "UPDATE invitations SET used_at = %s WHERE user_id = %s AND used_at IS NULL",
                (now.isoformat(), user_id),
            )
            invitation_id = str(uuid.uuid4())
            expires_at = (now + timedelta(hours=valid_hours)).isoformat()
            connection.execute(
                """INSERT INTO invitations(
                       id, user_id, token_hash, created_by_user_id, created_at, expires_at
                   ) VALUES (%s, %s, %s, %s, %s, %s)""",
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
                   WHERE i.token_hash = %s AND i.used_at IS NULL AND i.expires_at > %s""",
                (token_hash(raw_token), now),
            ).fetchone()
        return dict(row) if row else None

    def accept_invitation(
        self, raw_token: str, password_hash: str
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as connection:
            invitation = connection.execute(
                """SELECT * FROM invitations
                   WHERE token_hash = %s AND used_at IS NULL AND expires_at > %s
                   FOR UPDATE""",
                (token_hash(raw_token), now),
            ).fetchone()
            if not invitation:
                return None
            connection.execute(
                "UPDATE users SET password_hash = %s, enabled = TRUE WHERE id = %s",
                (password_hash, invitation["user_id"]),
            )
            connection.execute(
                "UPDATE invitations SET used_at = %s WHERE id = %s",
                (now, invitation["id"]),
            )
            user = connection.execute(
                "SELECT * FROM users WHERE id = %s", (invitation["user_id"],)
            ).fetchone()
        return dict(user)

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT,
                    role TEXT NOT NULL CHECK(role IN ('admin', 'member')),
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    platform TEXT,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT,
                    last_status TEXT,
                    owner_user_id TEXT REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS activity_records (
                    id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                    captured_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    keyboard_events INTEGER NOT NULL CHECK(keyboard_events >= 0),
                    mouse_clicks INTEGER NOT NULL CHECK(mouse_clicks >= 0),
                    mouse_distance INTEGER NOT NULL CHECK(mouse_distance >= 0),
                    active_app TEXT,
                    agent_version TEXT NOT NULL,
                    screenshot_path TEXT NOT NULL,
                    storage_version_id TEXT,
                    UNIQUE(device_id, captured_at)
                );

                CREATE TABLE IF NOT EXISTS invitations (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_by_user_id TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT
                );
                """
            )
            # Non-destructive migration for databases created by MVP 0.1.0.
            if "owner_user_id" not in self._columns(connection, "devices"):
                connection.execute(
                    "ALTER TABLE devices ADD COLUMN owner_user_id TEXT REFERENCES users(id) ON DELETE SET NULL"
                )
            if "storage_version_id" not in self._columns(connection, "activity_records"):
                connection.execute(
                    "ALTER TABLE activity_records ADD COLUMN storage_version_id TEXT"
                )
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_activity_device_captured
                ON activity_records(device_id, captured_at DESC);
                CREATE INDEX IF NOT EXISTS idx_devices_owner ON devices(owner_user_id);
                CREATE INDEX IF NOT EXISTS idx_invitations_user ON invitations(user_id);
                """
            )

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
                row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
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
                          (SELECT COUNT(*) FROM devices d WHERE d.owner_user_id = u.id) device_count
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

    def accept_invitation(self, raw_token: str, password_hash: str) -> dict[str, Any] | None:
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

    def create_device(
        self, name: str, owner_user_id: str | None = None
    ) -> tuple[dict[str, Any], str]:
        raw_token = secrets.token_urlsafe(32)
        device = {
            "id": str(uuid.uuid4()),
            "name": name.strip(),
            "created_at": utc_now(),
            "owner_user_id": owner_user_id,
        }
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO devices(id, name, token_hash, created_at, owner_user_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    device["id"],
                    device["name"],
                    token_hash(raw_token),
                    device["created_at"],
                    owner_user_id,
                ),
            )
        return device, raw_token

    def authenticate_device(self, raw_token: str) -> dict[str, Any] | None:
        if not raw_token:
            return None
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM devices WHERE token_hash = ? AND enabled = 1",
                (token_hash(raw_token),),
            ).fetchone()
        return dict(row) if row else None

    def touch_device(self, device_id: str, platform: str, status: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE devices SET last_seen_at = ?,
                          platform = COALESCE(NULLIF(?, ''), platform), last_status = ?
                   WHERE id = ?""",
                (utc_now(), platform[:80], status[:40], device_id),
            )

    def list_devices(self, owner_user_id: str | None = None) -> list[dict[str, Any]]:
        where = "" if owner_user_id is None else "WHERE d.owner_user_id = ?"
        parameters: tuple[Any, ...] = () if owner_user_id is None else (owner_user_id,)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT d.*, u.email AS owner_email, COUNT(a.id) AS record_count,
                       MAX(a.captured_at) AS latest_capture
                FROM devices d
                LEFT JOIN users u ON u.id = d.owner_user_id
                LEFT JOIN activity_records a ON a.device_id = d.id
                {where}
                GROUP BY d.id
                ORDER BY d.created_at DESC
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT d.*, u.email AS owner_email FROM devices d
                   LEFT JOIN users u ON u.id = d.owner_user_id WHERE d.id = ?""",
                (device_id,),
            ).fetchone()
        return dict(row) if row else None

    def set_device_enabled(self, device_id: str, enabled: bool) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE devices SET enabled = ? WHERE id = ?",
                (int(enabled), device_id),
            )

    def record_exists(self, record_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM activity_records WHERE id = ?", (record_id,)
            ).fetchone()
        return row is not None

    def add_record(self, record: dict[str, Any]) -> bool:
        try:
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO activity_records(
                        id, device_id, captured_at, received_at, keyboard_events,
                        mouse_clicks, mouse_distance, active_app, agent_version,
                        screenshot_path, storage_version_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["id"],
                        record["device_id"],
                        record["captured_at"],
                        utc_now(),
                        record["keyboard_events"],
                        record["mouse_clicks"],
                        record["mouse_distance"],
                        record.get("active_app"),
                        record["agent_version"],
                        record["screenshot_path"],
                        record.get("storage_version_id"),
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def list_records(self, device_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM activity_records WHERE device_id = ?
                   ORDER BY captured_at DESC LIMIT ?""",
                (device_id, min(max(limit, 1), 500)),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_record(self, record_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT a.*, d.owner_user_id FROM activity_records a
                   JOIN devices d ON d.id = a.device_id WHERE a.id = ?""",
                (record_id,),
            ).fetchone()
        return dict(row) if row else None

    def records_before(self, cutoff: str, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM activity_records WHERE captured_at < ?
                   ORDER BY captured_at ASC LIMIT ?""",
                (cutoff, min(max(limit, 1), 5_000)),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_record(self, record_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM activity_records WHERE id = ?", (record_id,))

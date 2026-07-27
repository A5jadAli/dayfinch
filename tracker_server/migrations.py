from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone


MigrationFn = Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: MigrationFn


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _create_schema(connection: sqlite3.Connection) -> None:
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

        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            description TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            created_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS project_members (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            added_at TEXT NOT NULL,
            PRIMARY KEY(project_id, user_id)
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
            owner_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            project_id TEXT REFERENCES projects(id) ON DELETE RESTRICT
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
            focused_seconds INTEGER NOT NULL DEFAULT 0,
            interactive_seconds INTEGER NOT NULL DEFAULT 0,
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

        CREATE TABLE IF NOT EXISTS audit_events (
            id TEXT PRIMARY KEY,
            actor_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            action TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT,
            occurred_at TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT ''
        );
        """
    )


def _upgrade_legacy_schema(connection: sqlite3.Connection) -> None:
    additions = (
        ("devices", "owner_user_id", "TEXT REFERENCES users(id) ON DELETE SET NULL"),
        ("devices", "project_id", "TEXT REFERENCES projects(id) ON DELETE RESTRICT"),
        ("activity_records", "storage_version_id", "TEXT"),
        ("activity_records", "focused_seconds", "INTEGER NOT NULL DEFAULT 0"),
        ("activity_records", "interactive_seconds", "INTEGER NOT NULL DEFAULT 0"),
    )
    for table, column, definition in additions:
        if column not in _columns(connection, table):
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _create_indexes(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_activity_device_captured
        ON activity_records(device_id, captured_at DESC);
        CREATE INDEX IF NOT EXISTS idx_devices_owner ON devices(owner_user_id);
        CREATE INDEX IF NOT EXISTS idx_devices_project ON devices(project_id);
        CREATE INDEX IF NOT EXISTS idx_invitations_user ON invitations(user_id);
        CREATE INDEX IF NOT EXISTS idx_project_members_user ON project_members(user_id);
        CREATE INDEX IF NOT EXISTS idx_audit_occurred ON audit_events(occurred_at DESC);
        """
    )


def _create_tasks_and_sessions(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'archived')),
            billable INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            created_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            UNIQUE(project_id, name)
        );

        CREATE TABLE IF NOT EXISTS work_sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            project_id TEXT REFERENCES projects(id) ON DELETE RESTRICT,
            task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
            status TEXT NOT NULL CHECK(status IN ('active', 'paused', 'stopped')),
            started_at TEXT NOT NULL,
            ended_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS work_session_segments (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES work_sessions(id) ON DELETE CASCADE,
            started_at TEXT NOT NULL,
            ended_at TEXT
        );
        """
    )
    activity_columns = _columns(connection, "activity_records")
    additions = (
        ("user_id", "TEXT REFERENCES users(id) ON DELETE SET NULL"),
        ("project_id", "TEXT REFERENCES projects(id) ON DELETE RESTRICT"),
        ("task_id", "TEXT REFERENCES tasks(id) ON DELETE SET NULL"),
        ("session_id", "TEXT REFERENCES work_sessions(id) ON DELETE SET NULL"),
    )
    for column, definition in additions:
        if column not in activity_columns:
            connection.execute(
                f"ALTER TABLE activity_records ADD COLUMN {column} {definition}"
            )
    connection.execute(
        """UPDATE activity_records
           SET project_id = COALESCE(project_id, (
                   SELECT d.project_id FROM devices d WHERE d.id = activity_records.device_id
               )),
               user_id = COALESCE(user_id, (
                   SELECT d.owner_user_id FROM devices d WHERE d.id = activity_records.device_id
               ))
           WHERE project_id IS NULL OR user_id IS NULL"""
    )
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_tasks_project_status
        ON tasks(project_id, status, name);
        CREATE INDEX IF NOT EXISTS idx_sessions_device_status
        ON work_sessions(device_id, status, started_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_one_open_per_device
        ON work_sessions(device_id) WHERE status IN ('active', 'paused');
        CREATE INDEX IF NOT EXISTS idx_sessions_user_started
        ON work_sessions(user_id, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_segments_session
        ON work_session_segments(session_id, started_at);
        CREATE INDEX IF NOT EXISTS idx_activity_project_captured
        ON activity_records(project_id, captured_at DESC);
        CREATE INDEX IF NOT EXISTS idx_activity_session
        ON activity_records(session_id);
        """
    )


MIGRATIONS = (
    Migration(1, "create_initial_schema", _create_schema),
    Migration(2, "upgrade_legacy_activity_schema", _upgrade_legacy_schema),
    Migration(3, "create_query_indexes", _create_indexes),
    Migration(4, "create_tasks_and_work_sessions", _create_tasks_and_sessions),
)


def apply_migrations(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version INTEGER PRIMARY KEY,
               name TEXT NOT NULL,
               applied_at TEXT NOT NULL
           )"""
    )
    applied = {
        row[0] for row in connection.execute("SELECT version FROM schema_migrations")
    }
    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        migration.apply(connection)
        connection.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.name, datetime.now(timezone.utc).isoformat()),
        )

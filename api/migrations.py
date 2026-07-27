from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from psycopg import Connection

MigrationFn = Callable[[Connection], None]


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: MigrationFn


def _create_schema(connection: Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id UUID PRIMARY KEY,
            email TEXT NOT NULL,
            password_hash TEXT,
            role TEXT NOT NULL CHECK(role IN ('admin', 'member')),
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_lower ON users(lower(email));

        CREATE TABLE IF NOT EXISTS projects (
            id UUID PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL,
            created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_projects_name_lower ON projects(lower(name));

        CREATE TABLE IF NOT EXISTS project_members (
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            added_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(project_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS devices (
            id UUID PRIMARY KEY,
            name TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            platform TEXT,
            created_at TIMESTAMPTZ NOT NULL,
            last_seen_at TIMESTAMPTZ,
            last_status TEXT,
            owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            project_id UUID REFERENCES projects(id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id UUID PRIMARY KEY,
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'archived')),
            billable BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL,
            created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_project_name_lower
        ON tasks(project_id, lower(name));

        CREATE TABLE IF NOT EXISTS work_sessions (
            id UUID PRIMARY KEY,
            user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            device_id UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            project_id UUID REFERENCES projects(id) ON DELETE RESTRICT,
            task_id UUID REFERENCES tasks(id) ON DELETE SET NULL,
            status TEXT NOT NULL CHECK(status IN ('active', 'paused', 'stopped')),
            started_at TIMESTAMPTZ NOT NULL,
            ended_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        );

        CREATE TABLE IF NOT EXISTS work_session_segments (
            id UUID PRIMARY KEY,
            session_id UUID NOT NULL REFERENCES work_sessions(id) ON DELETE CASCADE,
            started_at TIMESTAMPTZ NOT NULL,
            ended_at TIMESTAMPTZ
        );

        CREATE TABLE IF NOT EXISTS activity_records (
            id UUID PRIMARY KEY,
            device_id UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            project_id UUID REFERENCES projects(id) ON DELETE RESTRICT,
            task_id UUID REFERENCES tasks(id) ON DELETE SET NULL,
            session_id UUID REFERENCES work_sessions(id) ON DELETE SET NULL,
            captured_at TIMESTAMPTZ NOT NULL,
            received_at TIMESTAMPTZ NOT NULL,
            keyboard_events INTEGER NOT NULL CHECK(keyboard_events >= 0),
            mouse_clicks INTEGER NOT NULL CHECK(mouse_clicks >= 0),
            mouse_distance BIGINT NOT NULL CHECK(mouse_distance >= 0),
            active_app TEXT,
            agent_version TEXT NOT NULL,
            screenshot_path TEXT NOT NULL,
            storage_version_id TEXT,
            focused_seconds INTEGER NOT NULL DEFAULT 0,
            interactive_seconds INTEGER NOT NULL DEFAULT 0,
            UNIQUE(device_id, captured_at)
        );

        CREATE TABLE IF NOT EXISTS invitations (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            created_by_user_id UUID NOT NULL REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ
        );

        CREATE TABLE IF NOT EXISTS audit_events (
            id UUID PRIMARY KEY,
            actor_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            action TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id UUID,
            occurred_at TIMESTAMPTZ NOT NULL,
            details TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_activity_device_captured
        ON activity_records(device_id, captured_at DESC);
        CREATE INDEX IF NOT EXISTS idx_activity_project_captured
        ON activity_records(project_id, captured_at DESC);
        CREATE INDEX IF NOT EXISTS idx_activity_session ON activity_records(session_id);
        CREATE INDEX IF NOT EXISTS idx_devices_owner ON devices(owner_user_id);
        CREATE INDEX IF NOT EXISTS idx_devices_project ON devices(project_id);
        CREATE INDEX IF NOT EXISTS idx_invitations_user ON invitations(user_id);
        CREATE INDEX IF NOT EXISTS idx_project_members_user ON project_members(user_id);
        CREATE INDEX IF NOT EXISTS idx_audit_occurred ON audit_events(occurred_at DESC);
        CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project_id, status, name);
        CREATE INDEX IF NOT EXISTS idx_sessions_device_status
        ON work_sessions(device_id, status, started_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_one_open_per_device
        ON work_sessions(device_id) WHERE status IN ('active', 'paused');
        CREATE INDEX IF NOT EXISTS idx_sessions_user_started
        ON work_sessions(user_id, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_segments_session
        ON work_session_segments(session_id, started_at);
        """
    )


def _create_timesheets(connection: Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS timesheets (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            period_start DATE NOT NULL,
            period_end DATE NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('submitted', 'approved', 'rejected')),
            submitted_at TIMESTAMPTZ NOT NULL,
            reviewed_at TIMESTAMPTZ,
            reviewed_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            review_note TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            CHECK(period_end >= period_start),
            CHECK(period_end - period_start <= 31),
            UNIQUE(user_id, period_start, period_end)
        );
        CREATE INDEX IF NOT EXISTS idx_timesheets_status_period
        ON timesheets(status, period_start DESC, period_end DESC);
        CREATE INDEX IF NOT EXISTS idx_timesheets_user_period
        ON timesheets(user_id, period_start DESC, period_end DESC);
        """
    )


MIGRATIONS = (
    Migration(1, "create_postgresql_schema", _create_schema),
    Migration(2, "create_timesheets", _create_timesheets),
)


def apply_migrations(connection: Connection) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtext('dayfinch_schema_migrations'))"
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version INTEGER PRIMARY KEY,
               name TEXT NOT NULL,
               applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    applied = {
        row["version"]
        for row in connection.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()
    }
    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        migration.apply(connection)
        connection.execute(
            "INSERT INTO schema_migrations(version, name) VALUES (%s, %s)",
            (migration.version, migration.name),
        )

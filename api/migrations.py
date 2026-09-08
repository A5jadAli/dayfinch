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


def _add_website_and_automation(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE activity_records
            ADD COLUMN IF NOT EXISTS active_url TEXT,
            ADD COLUMN IF NOT EXISTS automation_suspected BOOLEAN NOT NULL DEFAULT FALSE;
        """
    )


def _add_durable_state_events(connection: Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_state_events (
            id UUID NOT NULL,
            device_id UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            observed_at TIMESTAMPTZ NOT NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL CHECK(status IN ('active', 'paused', 'stopped')),
            task_id UUID REFERENCES tasks(id) ON DELETE SET NULL,
            idle_seconds INTEGER NOT NULL DEFAULT 0 CHECK(idle_seconds >= 0),
            heartbeat_interval_seconds INTEGER NOT NULL
                CHECK(heartbeat_interval_seconds BETWEEN 15 AND 3600),
            session_id UUID REFERENCES work_sessions(id) ON DELETE SET NULL,
            PRIMARY KEY(device_id, id)
        );
        CREATE INDEX IF NOT EXISTS idx_agent_state_device_observed
        ON agent_state_events(device_id, observed_at DESC);
        CREATE INDEX IF NOT EXISTS idx_agent_state_received
        ON agent_state_events(received_at);
        """
    )


def _create_workforce_platform(connection: Connection) -> None:
    """Expand the original tracker schema into the Dayfinch workforce suite."""
    connection.execute(
        """
        ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
        ALTER TABLE users ADD CONSTRAINT users_role_check
            CHECK(role IN ('admin', 'manager', 'member', 'viewer'));
        ALTER TABLE users
            ADD COLUMN IF NOT EXISTS full_name TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT 'UTC',
            ADD COLUMN IF NOT EXISTS pay_rate NUMERIC(12,2) NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS bill_rate NUMERIC(12,2) NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS weekly_limit_minutes INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS employment_type TEXT NOT NULL DEFAULT 'employee';

        ALTER TABLE projects
            ADD COLUMN IF NOT EXISTS color TEXT NOT NULL DEFAULT '#6d5dfc',
            ADD COLUMN IF NOT EXISTS budget_type TEXT NOT NULL DEFAULT 'none',
            ADD COLUMN IF NOT EXISTS budget_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS budget_minutes INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS billable_rate NUMERIC(12,2) NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;

        ALTER TABLE activity_records
            ADD COLUMN IF NOT EXISTS activity_percent INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS screenshot_blurred BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'desktop';

        CREATE TABLE IF NOT EXISTS organization_settings (
            id SMALLINT PRIMARY KEY DEFAULT 1 CHECK(id = 1),
            name TEXT NOT NULL DEFAULT 'Dayfinch Workspace',
            timezone TEXT NOT NULL DEFAULT 'UTC',
            currency TEXT NOT NULL DEFAULT 'USD',
            week_starts_on SMALLINT NOT NULL DEFAULT 1 CHECK(week_starts_on BETWEEN 0 AND 6),
            screenshot_frequency INTEGER NOT NULL DEFAULT 2 CHECK(screenshot_frequency BETWEEN 0 AND 3),
            screenshot_blur BOOLEAN NOT NULL DEFAULT FALSE,
            track_apps BOOLEAN NOT NULL DEFAULT TRUE,
            track_urls BOOLEAN NOT NULL DEFAULT TRUE,
            allow_manual_time BOOLEAN NOT NULL DEFAULT TRUE,
            require_time_approval BOOLEAN NOT NULL DEFAULT TRUE,
            allow_screenshot_delete BOOLEAN NOT NULL DEFAULT TRUE,
            idle_timeout_minutes INTEGER NOT NULL DEFAULT 20,
            retention_days INTEGER NOT NULL DEFAULT 90,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO organization_settings(id) VALUES (1) ON CONFLICT DO NOTHING;

        CREATE TABLE IF NOT EXISTS clients (
            id UUID PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL DEFAULT '',
            address TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL,
            UNIQUE(name)
        );
        ALTER TABLE projects ADD COLUMN IF NOT EXISTS client_id UUID REFERENCES clients(id) ON DELETE SET NULL;

        CREATE TABLE IF NOT EXISTS teams (
            id UUID PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            lead_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL
        );
        CREATE TABLE IF NOT EXISTS team_members (
            team_id UUID NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            PRIMARY KEY(team_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS manual_time_entries (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
            task_id UUID REFERENCES tasks(id) ON DELETE SET NULL,
            started_at TIMESTAMPTZ NOT NULL,
            ended_at TIMESTAMPTZ NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
            reviewed_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            reviewed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL,
            CHECK(ended_at > started_at)
        );

        CREATE TABLE IF NOT EXISTS shifts (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            project_id UUID REFERENCES projects(id) ON DELETE SET NULL,
            starts_at TIMESTAMPTZ NOT NULL,
            ends_at TIMESTAMPTZ NOT NULL,
            minimum_minutes INTEGER NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT '',
            published BOOLEAN NOT NULL DEFAULT TRUE,
            created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL,
            CHECK(ends_at > starts_at)
        );

        CREATE TABLE IF NOT EXISTS time_off_requests (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            category TEXT NOT NULL DEFAULT 'paid',
            starts_on DATE NOT NULL,
            ends_on DATE NOT NULL,
            minutes INTEGER NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','denied')),
            reviewed_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            reviewed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL,
            CHECK(ends_on >= starts_on)
        );

        CREATE TABLE IF NOT EXISTS expenses (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            project_id UUID REFERENCES projects(id) ON DELETE SET NULL,
            incurred_on DATE NOT NULL,
            category TEXT NOT NULL DEFAULT 'other',
            amount NUMERIC(14,2) NOT NULL CHECK(amount >= 0),
            currency TEXT NOT NULL DEFAULT 'USD',
            description TEXT NOT NULL DEFAULT '',
            receipt_key TEXT,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected','reimbursed')),
            reviewed_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            reviewed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL
        );

        CREATE TABLE IF NOT EXISTS invoices (
            id UUID PRIMARY KEY,
            number TEXT NOT NULL UNIQUE,
            client_id UUID REFERENCES clients(id) ON DELETE SET NULL,
            issued_on DATE NOT NULL,
            due_on DATE NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','sent','paid','void','overdue')),
            currency TEXT NOT NULL DEFAULT 'USD',
            tax_percent NUMERIC(6,3) NOT NULL DEFAULT 0,
            discount_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT '',
            created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL
        );
        CREATE TABLE IF NOT EXISTS invoice_lines (
            id UUID PRIMARY KEY,
            invoice_id UUID NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
            project_id UUID REFERENCES projects(id) ON DELETE SET NULL,
            description TEXT NOT NULL,
            quantity NUMERIC(12,2) NOT NULL DEFAULT 1,
            unit_price NUMERIC(14,2) NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS payroll_payments (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            period_start DATE NOT NULL,
            period_end DATE NOT NULL,
            regular_minutes INTEGER NOT NULL DEFAULT 0,
            overtime_minutes INTEGER NOT NULL DEFAULT 0,
            gross_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'USD',
            provider TEXT NOT NULL DEFAULT 'manual',
            status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','processing','paid','failed')),
            paid_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id UUID PRIMARY KEY,
            user_id UUID REFERENCES users(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            read_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_manual_time_user_started ON manual_time_entries(user_id, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_shifts_user_starts ON shifts(user_id, starts_at);
        CREATE INDEX IF NOT EXISTS idx_time_off_status ON time_off_requests(status, starts_on);
        CREATE INDEX IF NOT EXISTS idx_expenses_status ON expenses(status, incurred_on DESC);
        CREATE INDEX IF NOT EXISTS idx_notifications_user_created ON notifications(user_id, created_at DESC);
        ALTER TABLE agent_state_events ADD COLUMN IF NOT EXISTS project_id UUID REFERENCES projects(id) ON DELETE SET NULL;
        """
    )


def _create_field_and_integrations(connection: Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS geofences (
            id UUID PRIMARY KEY,
            name TEXT NOT NULL,
            project_id UUID REFERENCES projects(id) ON DELETE SET NULL,
            latitude DOUBLE PRECISION NOT NULL CHECK(latitude BETWEEN -90 AND 90),
            longitude DOUBLE PRECISION NOT NULL CHECK(longitude BETWEEN -180 AND 180),
            radius_meters INTEGER NOT NULL DEFAULT 200 CHECK(radius_meters BETWEEN 25 AND 100000),
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL
        );
        CREATE TABLE IF NOT EXISTS location_events (
            id UUID PRIMARY KEY,
            device_id UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            session_id UUID REFERENCES work_sessions(id) ON DELETE SET NULL,
            recorded_at TIMESTAMPTZ NOT NULL,
            latitude DOUBLE PRECISION NOT NULL CHECK(latitude BETWEEN -90 AND 90),
            longitude DOUBLE PRECISION NOT NULL CHECK(longitude BETWEEN -180 AND 180),
            accuracy_meters DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK(accuracy_meters >= 0),
            geofence_id UUID REFERENCES geofences(id) ON DELETE SET NULL,
            event_type TEXT NOT NULL DEFAULT 'position' CHECK(event_type IN ('position','enter','exit')),
            created_at TIMESTAMPTZ NOT NULL,
            UNIQUE(device_id, recorded_at)
        );
        CREATE TABLE IF NOT EXISTS integrations (
            id UUID PRIMARY KEY,
            provider TEXT NOT NULL,
            display_name TEXT NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            webhook_url TEXT NOT NULL DEFAULT '',
            secret_ciphertext TEXT NOT NULL DEFAULT '',
            settings_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            UNIQUE(provider, display_name)
        );
        CREATE TABLE IF NOT EXISTS holidays (
            id UUID PRIMARY KEY,
            name TEXT NOT NULL,
            holiday_date DATE NOT NULL,
            paid_minutes INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL,
            UNIQUE(name, holiday_date)
        );
        CREATE TABLE IF NOT EXISTS work_breaks (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            session_id UUID REFERENCES work_sessions(id) ON DELETE SET NULL,
            started_at TIMESTAMPTZ NOT NULL,
            ended_at TIMESTAMPTZ,
            paid BOOLEAN NOT NULL DEFAULT FALSE,
            notes TEXT NOT NULL DEFAULT '',
            CHECK(ended_at IS NULL OR ended_at > started_at)
        );
        CREATE INDEX IF NOT EXISTS idx_location_device_recorded ON location_events(device_id, recorded_at DESC);
        CREATE INDEX IF NOT EXISTS idx_location_user_recorded ON location_events(user_id, recorded_at DESC);
        CREATE INDEX IF NOT EXISTS idx_geofences_project ON geofences(project_id);
        CREATE INDEX IF NOT EXISTS idx_breaks_user_started ON work_breaks(user_id, started_at DESC);
        """
    )


def _create_advanced_controls(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE users
            ADD COLUMN IF NOT EXISTS daily_limit_minutes INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS manage_it BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS require_timesheet_approval BOOLEAN NOT NULL DEFAULT TRUE;
        ALTER TABLE work_sessions ADD COLUMN IF NOT EXISTS note TEXT NOT NULL DEFAULT '';
        ALTER TABLE agent_state_events ADD COLUMN IF NOT EXISTS note TEXT NOT NULL DEFAULT '';
        ALTER TABLE organization_settings
            ADD COLUMN IF NOT EXISTS address TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS tax_id TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS require_edit_reason BOOLEAN NOT NULL DEFAULT TRUE,
            ADD COLUMN IF NOT EXISTS allow_keep_idle BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS pay_period TEXT NOT NULL DEFAULT 'weekly',
            ADD COLUMN IF NOT EXISTS require_two_factor BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS sso_provider TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS sso_domain TEXT NOT NULL DEFAULT '';
        ALTER TABLE geofences
            ADD COLUMN IF NOT EXISTS enter_action TEXT NOT NULL DEFAULT 'none',
            ADD COLUMN IF NOT EXISTS exit_action TEXT NOT NULL DEFAULT 'none';

        CREATE TABLE IF NOT EXISTS global_todos (
            id UUID PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            add_to_future_projects BOOLEAN NOT NULL DEFAULT FALSE,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL
        );
        CREATE TABLE IF NOT EXISTS project_todos (
            todo_id UUID NOT NULL REFERENCES global_todos(id) ON DELETE CASCADE,
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            assigned_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            completed_at TIMESTAMPTZ,
            completed_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            PRIMARY KEY(todo_id, project_id)
        );
        CREATE TABLE IF NOT EXISTS scheduled_reports (
            id UUID PRIMARY KEY,
            name TEXT NOT NULL,
            report_type TEXT NOT NULL,
            frequency TEXT NOT NULL CHECK(frequency IN ('weekly','monthly')),
            recipients TEXT NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            last_sent_at TIMESTAMPTZ,
            next_send_at TIMESTAMPTZ,
            created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_scheduled_reports_next ON scheduled_reports(enabled,next_send_at);
        """
    )


def _create_two_factor_authentication(connection: Connection) -> None:
    connection.execute(
        """ALTER TABLE users
           ADD COLUMN IF NOT EXISTS totp_secret TEXT,
           ADD COLUMN IF NOT EXISTS two_factor_enabled BOOLEAN NOT NULL DEFAULT FALSE;"""
    )


def _create_automatic_pay_periods(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE timesheets DROP CONSTRAINT IF EXISTS timesheets_status_check;
        ALTER TABLE timesheets ADD CONSTRAINT timesheets_status_check
            CHECK(status IN ('open','submitted','approved','rejected'));
        ALTER TABLE timesheets ALTER COLUMN submitted_at DROP NOT NULL;
        """
    )


def _create_payroll_provider_delivery(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE payroll_payments
            ADD COLUMN IF NOT EXISTS external_reference TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS failure_reason TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
        UPDATE payroll_payments SET updated_at=created_at WHERE updated_at IS NULL;
        ALTER TABLE payroll_payments ALTER COLUMN updated_at SET NOT NULL;
        ALTER TABLE payroll_payments ALTER COLUMN updated_at SET DEFAULT CURRENT_TIMESTAMP;
        """
    )


def _make_tracking_limits_opt_in(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE users ALTER COLUMN daily_limit_minutes SET DEFAULT 0;
        ALTER TABLE users ALTER COLUMN weekly_limit_minutes SET DEFAULT 0;
        UPDATE users SET daily_limit_minutes=0 WHERE daily_limit_minutes=480;
        UPDATE users SET weekly_limit_minutes=0 WHERE weekly_limit_minutes=2400;
        """
    )


def _create_encrypted_invoice_documents(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE invoices
            ADD COLUMN IF NOT EXISTS encrypted_document_key TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS document_version_id TEXT,
            ADD COLUMN IF NOT EXISTS encrypted_document_sha256 TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS document_sealed_at TIMESTAMPTZ;
        """
    )


def _create_usage_timeline(connection: Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_records (
            id UUID PRIMARY KEY,
            device_id UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            project_id UUID REFERENCES projects(id) ON DELETE SET NULL,
            task_id UUID REFERENCES tasks(id) ON DELETE SET NULL,
            session_id UUID REFERENCES work_sessions(id) ON DELETE SET NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            active_app TEXT,
            active_url TEXT,
            focused_seconds INTEGER NOT NULL DEFAULT 0 CHECK(focused_seconds BETWEEN 0 AND 3600),
            received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(device_id,id)
        );
        CREATE INDEX IF NOT EXISTS idx_usage_user_observed ON usage_records(user_id,observed_at DESC);
        CREATE INDEX IF NOT EXISTS idx_usage_project_observed ON usage_records(project_id,observed_at DESC);
        """
    )


MIGRATIONS = (
    Migration(1, "create_postgresql_schema", _create_schema),
    Migration(2, "create_timesheets", _create_timesheets),
    Migration(3, "add_website_and_automation", _add_website_and_automation),
    Migration(4, "add_durable_state_events", _add_durable_state_events),
    Migration(5, "create_workforce_platform", _create_workforce_platform),
    Migration(6, "create_field_and_integrations", _create_field_and_integrations),
    Migration(7, "create_advanced_controls", _create_advanced_controls),
    Migration(8, "create_two_factor_authentication", _create_two_factor_authentication),
    Migration(9, "create_automatic_pay_periods", _create_automatic_pay_periods),
    Migration(
        10, "create_payroll_provider_delivery", _create_payroll_provider_delivery
    ),
    Migration(11, "make_tracking_limits_opt_in", _make_tracking_limits_opt_in),
    Migration(
        12, "create_encrypted_invoice_documents", _create_encrypted_invoice_documents
    ),
    Migration(13, "create_usage_timeline", _create_usage_timeline),
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

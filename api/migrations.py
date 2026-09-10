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
            tracker_kind TEXT NOT NULL DEFAULT 'desktop'
                CHECK(tracker_kind IN ('desktop','mobile','web')),
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
            overtime_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            weekly_overtime_minutes INTEGER NOT NULL DEFAULT 2400
                CHECK(weekly_overtime_minutes BETWEEN 0 AND 10080),
            overtime_multiplier NUMERIC(5,2) NOT NULL DEFAULT 1.50
                CHECK(overtime_multiplier BETWEEN 1 AND 10),
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
            pay_rate_snapshot NUMERIC(12,2) NOT NULL DEFAULT 0,
            overtime_multiplier_snapshot NUMERIC(5,2) NOT NULL DEFAULT 1.50,
            gross_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'USD',
            provider TEXT NOT NULL DEFAULT 'manual',
            recipient TEXT NOT NULL DEFAULT '',
            recipient_currency TEXT NOT NULL DEFAULT '',
            source_timesheet_id UUID REFERENCES timesheets(id) ON DELETE RESTRICT,
            delivery_started_at TIMESTAMPTZ,
            status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','processing','paid','failed','reversed')),
            paid_at TIMESTAMPTZ,
            reversed_at TIMESTAMPTZ,
            next_reconcile_at TIMESTAMPTZ,
            reconcile_until TIMESTAMPTZ,
            reconcile_attempts INTEGER NOT NULL DEFAULT 0 CHECK(reconcile_attempts BETWEEN 0 AND 20),
            last_reconcile_error TEXT NOT NULL DEFAULT '',
            provider_event_at TIMESTAMPTZ,
            provider_status TEXT NOT NULL DEFAULT '',
            provider_failure_code TEXT NOT NULL DEFAULT '',
            provider_failure_description TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payroll_destinations (
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider TEXT NOT NULL CHECK(provider IN ('paypal','wise')),
            recipient TEXT NOT NULL,
            currency TEXT NOT NULL DEFAULT '',
            confirmed_at TIMESTAMPTZ NOT NULL,
            updated_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id,provider)
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


def _create_login_throttle(connection: Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS login_attempts (
            id BIGSERIAL PRIMARY KEY,
            identity_hash TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            attempted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_login_attempt_identity_time
            ON login_attempts(identity_hash, attempted_at DESC);
        CREATE INDEX IF NOT EXISTS idx_login_attempt_source_time
            ON login_attempts(source_hash, attempted_at DESC);
        """
    )


def _index_login_throttle_cleanup(connection: Connection) -> None:
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_login_attempt_time ON login_attempts(attempted_at)"
    )


def _create_background_job_leases(connection: Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS background_job_leases (
            name TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            lease_until TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_background_job_lease_until
            ON background_job_leases(lease_until);
        """
    )


def _create_oidc_identities(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE users
            ADD COLUMN IF NOT EXISTS sso_issuer TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS sso_subject TEXT NOT NULL DEFAULT '';
        CREATE UNIQUE INDEX IF NOT EXISTS uq_users_sso_identity
            ON users(sso_issuer, sso_subject)
            WHERE sso_issuer <> '' AND sso_subject <> '';
        """
    )


def _create_saml_replay_guard(connection: Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS saml_assertion_replays (
            response_id TEXT PRIMARY KEY,
            assertion_id TEXT NOT NULL UNIQUE,
            expires_at TIMESTAMPTZ NOT NULL,
            consumed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_saml_assertion_replay_expiry
            ON saml_assertion_replays(expires_at);
        """
    )


def _create_scim_identities(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE users
            ADD COLUMN IF NOT EXISTS scim_external_id TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS scim_updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_users_scim_external_id
            ON users(scim_external_id) WHERE scim_external_id <> '';
        """
    )


def _create_project_roles(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE project_members
            ADD COLUMN IF NOT EXISTS project_role TEXT NOT NULL DEFAULT 'worker';
        ALTER TABLE project_members DROP CONSTRAINT IF EXISTS project_members_role_check;
        ALTER TABLE project_members ADD CONSTRAINT project_members_role_check
            CHECK(project_role IN ('worker','manager','viewer'));
        UPDATE project_members pm SET project_role='viewer'
          FROM users u WHERE u.id=pm.user_id AND u.role='viewer';
        CREATE INDEX IF NOT EXISTS idx_project_members_user_role
            ON project_members(user_id,project_role,project_id);
        """
    )


def _replace_global_viewers_with_project_viewers(connection: Connection) -> None:
    connection.execute(
        """
        UPDATE project_members pm SET project_role='viewer'
          FROM users u WHERE u.id=pm.user_id AND u.role='viewer';
        UPDATE users SET role='member' WHERE role='viewer';
        """
    )


def _create_team_lead_permissions(connection: Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS team_leads (
            team_id UUID NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            can_approve_timesheets BOOLEAN NOT NULL DEFAULT TRUE,
            can_approve_manual_time BOOLEAN NOT NULL DEFAULT TRUE,
            can_approve_time_off BOOLEAN NOT NULL DEFAULT TRUE,
            can_manage_schedules BOOLEAN NOT NULL DEFAULT TRUE,
            can_manage_projects BOOLEAN NOT NULL DEFAULT FALSE,
            can_manage_members BOOLEAN NOT NULL DEFAULT FALSE,
            can_manage_financials BOOLEAN NOT NULL DEFAULT FALSE,
            PRIMARY KEY(team_id,user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_team_leads_user ON team_leads(user_id,team_id);
        INSERT INTO team_leads(team_id,user_id)
            SELECT id,lead_user_id FROM teams WHERE lead_user_id IS NOT NULL
            ON CONFLICT(team_id,user_id) DO NOTHING;
        INSERT INTO team_members(team_id,user_id)
            SELECT id,lead_user_id FROM teams WHERE lead_user_id IS NOT NULL
            ON CONFLICT(team_id,user_id) DO NOTHING;
        """
    )


def _create_team_projects(connection: Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS team_projects (
            team_id UUID NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            PRIMARY KEY(team_id,project_id)
        );
        CREATE INDEX IF NOT EXISTS idx_team_projects_project
            ON team_projects(project_id,team_id);
        """
    )


def _create_scim_groups(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE teams
            ADD COLUMN IF NOT EXISTS scim_managed BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS scim_external_id TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS scim_updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_teams_scim_external_id
            ON teams(scim_external_id) WHERE scim_external_id <> '';
        """
    )


def _index_active_work_lifecycle(connection: Connection) -> None:
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sessions_project_open
            ON work_sessions(project_id,user_id)
            WHERE status IN ('active','paused');
        CREATE INDEX IF NOT EXISTS idx_sessions_user_open
            ON work_sessions(user_id)
            WHERE status IN ('active','paused');
        CREATE INDEX IF NOT EXISTS idx_segments_open_session
            ON work_session_segments(session_id)
            WHERE ended_at IS NULL;
        CREATE INDEX IF NOT EXISTS idx_breaks_open_session
            ON work_breaks(session_id)
            WHERE ended_at IS NULL;
        CREATE INDEX IF NOT EXISTS idx_devices_owner_project
            ON devices(owner_user_id,project_id);
        """
    )


def _create_saved_report_filters(connection: Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS saved_report_filters (
            id UUID PRIMARY KEY,
            owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            configuration JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_saved_report_filter_owner_name
            ON saved_report_filters(owner_user_id, lower(name));
        CREATE INDEX IF NOT EXISTS idx_saved_report_filter_owner_updated
            ON saved_report_filters(owner_user_id, updated_at DESC);
        """
    )


def _create_team_invoices(connection: Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS team_invoices (
            id UUID PRIMARY KEY,
            number TEXT NOT NULL UNIQUE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            issued_on DATE NOT NULL,
            due_on DATE NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft'
                CHECK(status IN ('draft','submitted','partially_paid','paid','void')),
            currency TEXT NOT NULL DEFAULT 'USD',
            purchase_order TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            paid_amount NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK(paid_amount >= 0),
            submitted_at TIMESTAMPTZ,
            encrypted_document_key TEXT NOT NULL DEFAULT '',
            document_version_id TEXT,
            encrypted_document_sha256 TEXT NOT NULL DEFAULT '',
            document_sealed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            CHECK(due_on >= issued_on)
        );
        CREATE TABLE IF NOT EXISTS team_invoice_lines (
            id UUID PRIMARY KEY,
            invoice_id UUID NOT NULL REFERENCES team_invoices(id) ON DELETE CASCADE,
            project_id UUID REFERENCES projects(id) ON DELETE SET NULL,
            description TEXT NOT NULL,
            quantity NUMERIC(12,2) NOT NULL CHECK(quantity > 0),
            unit_price NUMERIC(14,2) NOT NULL CHECK(unit_price >= 0),
            source TEXT NOT NULL DEFAULT 'manual' CHECK(source IN ('manual','tracked')),
            period_start DATE,
            period_end DATE
        );
        CREATE TABLE IF NOT EXISTS team_invoice_time_sources (
            invoice_line_id UUID NOT NULL REFERENCES team_invoice_lines(id) ON DELETE CASCADE,
            source_type TEXT NOT NULL CHECK(source_type IN ('segment','manual')),
            source_id UUID NOT NULL,
            seconds INTEGER NOT NULL CHECK(seconds > 0),
            PRIMARY KEY(source_type,source_id)
        );
        CREATE TABLE IF NOT EXISTS team_invoice_payments (
            id UUID PRIMARY KEY,
            invoice_id UUID NOT NULL REFERENCES team_invoices(id) ON DELETE CASCADE,
            amount NUMERIC(14,2) NOT NULL CHECK(amount > 0),
            paid_on DATE NOT NULL,
            reference TEXT NOT NULL DEFAULT '',
            recorded_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_team_invoices_user_issued
            ON team_invoices(user_id,issued_on DESC);
        CREATE INDEX IF NOT EXISTS idx_team_invoices_status_due
            ON team_invoices(status,due_on);
        CREATE INDEX IF NOT EXISTS idx_team_invoice_payments_invoice
            ON team_invoice_payments(invoice_id,created_at);
        """
    )


def _create_automatic_tracking_policies(connection: Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS automatic_tracking_policies (
            id UUID PRIMARY KEY,
            name TEXT NOT NULL,
            rule_type TEXT NOT NULL CHECK(rule_type IN ('fixed_schedule','shifts')),
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
            wait_for_activity BOOLEAN NOT NULL DEFAULT FALSE,
            schedule JSONB NOT NULL DEFAULT '{}'::jsonb,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            CHECK(rule_type <> 'fixed_schedule' OR schedule <> '{}'::jsonb)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_automatic_tracking_policy_name
            ON automatic_tracking_policies(lower(name));
        CREATE TABLE IF NOT EXISTS automatic_tracking_assignments (
            policy_id UUID NOT NULL REFERENCES automatic_tracking_policies(id)
                ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            consent_status TEXT NOT NULL DEFAULT 'pending'
                CHECK(consent_status IN ('pending','accepted','declined')),
            assigned_at TIMESTAMPTZ NOT NULL,
            responded_at TIMESTAMPTZ,
            PRIMARY KEY(policy_id,user_id),
            UNIQUE(user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_automatic_tracking_assignments_policy
            ON automatic_tracking_assignments(policy_id,consent_status);
        """
    )


def _enforce_single_active_timer(connection: Connection) -> None:
    connection.execute(
        """
        WITH duplicate_sessions AS (
            SELECT id,updated_at,
                   row_number() OVER (
                       PARTITION BY user_id ORDER BY updated_at DESC,id DESC
                   ) position
            FROM work_sessions
            WHERE user_id IS NOT NULL AND status IN ('active','paused')
        )
        UPDATE work_session_segments segment
        SET ended_at=GREATEST(segment.started_at,duplicate_sessions.updated_at)
        FROM duplicate_sessions
        WHERE segment.session_id=duplicate_sessions.id
          AND duplicate_sessions.position>1 AND segment.ended_at IS NULL;

        WITH duplicate_sessions AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY user_id ORDER BY updated_at DESC,id DESC
                   ) position
            FROM work_sessions
            WHERE user_id IS NOT NULL AND status IN ('active','paused')
        )
        UPDATE work_sessions session
        SET status='stopped',ended_at=COALESCE(session.ended_at,session.updated_at)
        FROM duplicate_sessions
        WHERE session.id=duplicate_sessions.id AND duplicate_sessions.position>1;

        CREATE UNIQUE INDEX IF NOT EXISTS uq_work_sessions_one_active_per_user
            ON work_sessions(user_id) WHERE status IN ('active','paused');
        """
    )


def _add_scheduled_report_formats(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE scheduled_reports
            ADD COLUMN IF NOT EXISTS delivery_format TEXT NOT NULL DEFAULT 'csv';
        ALTER TABLE scheduled_reports
            DROP CONSTRAINT IF EXISTS scheduled_reports_delivery_format_check;
        ALTER TABLE scheduled_reports
            ADD CONSTRAINT scheduled_reports_delivery_format_check
            CHECK(delivery_format IN ('csv','pdf'));
        """
    )


def _add_scheduled_report_calendar(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE scheduled_reports
            DROP CONSTRAINT IF EXISTS scheduled_reports_frequency_check;
        ALTER TABLE scheduled_reports
            ADD CONSTRAINT scheduled_reports_frequency_check
            CHECK(frequency IN ('daily','weekly','monthly'));
        ALTER TABLE scheduled_reports
            ADD COLUMN IF NOT EXISTS delivery_hour SMALLINT NOT NULL DEFAULT 9,
            ADD COLUMN IF NOT EXISTS delivery_minute SMALLINT NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS schedule_weekday SMALLINT NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS schedule_month_day SMALLINT NOT NULL DEFAULT 1;
        ALTER TABLE scheduled_reports
            DROP CONSTRAINT IF EXISTS scheduled_reports_delivery_hour_check,
            DROP CONSTRAINT IF EXISTS scheduled_reports_delivery_minute_check,
            DROP CONSTRAINT IF EXISTS scheduled_reports_schedule_weekday_check,
            DROP CONSTRAINT IF EXISTS scheduled_reports_schedule_month_day_check;
        ALTER TABLE scheduled_reports
            ADD CONSTRAINT scheduled_reports_delivery_hour_check
                CHECK(delivery_hour BETWEEN 0 AND 23),
            ADD CONSTRAINT scheduled_reports_delivery_minute_check
                CHECK(delivery_minute BETWEEN 0 AND 59),
            ADD CONSTRAINT scheduled_reports_schedule_weekday_check
                CHECK(schedule_weekday BETWEEN 0 AND 6),
            ADD CONSTRAINT scheduled_reports_schedule_month_day_check
                CHECK(schedule_month_day IN (-1,1,15));
        """
    )


def _harden_scheduled_report_delivery(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE scheduled_reports
            ADD COLUMN IF NOT EXISTS range_preset TEXT NOT NULL DEFAULT 'previous_period',
            ADD COLUMN IF NOT EXISTS consecutive_failures INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS last_failed_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS last_error_code TEXT NOT NULL DEFAULT '';
        ALTER TABLE scheduled_reports
            DROP CONSTRAINT IF EXISTS scheduled_reports_range_preset_check,
            DROP CONSTRAINT IF EXISTS scheduled_reports_consecutive_failures_check,
            DROP CONSTRAINT IF EXISTS scheduled_reports_last_error_code_check;
        ALTER TABLE scheduled_reports
            ADD CONSTRAINT scheduled_reports_range_preset_check CHECK(
                range_preset IN (
                    'previous_period','previous_day','previous_week',
                    'previous_month','last_7_days','last_30_days'
                )
            ),
            ADD CONSTRAINT scheduled_reports_consecutive_failures_check
                CHECK(consecutive_failures BETWEEN 0 AND 1000000),
            ADD CONSTRAINT scheduled_reports_last_error_code_check
                CHECK(length(last_error_code) <= 64);
        CREATE INDEX IF NOT EXISTS idx_activity_captured
            ON activity_records(captured_at DESC);
        CREATE INDEX IF NOT EXISTS idx_segments_started
            ON work_session_segments(started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_manual_time_started
            ON manual_time_entries(started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_shifts_starts
            ON shifts(starts_at DESC);
        CREATE INDEX IF NOT EXISTS idx_expenses_incurred
            ON expenses(incurred_on DESC);
        """
    )


def _claim_scheduled_report_delivery(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE scheduled_reports
            ADD COLUMN IF NOT EXISTS delivery_claim_token UUID,
            ADD COLUMN IF NOT EXISTS delivery_claim_until TIMESTAMPTZ;
        ALTER TABLE scheduled_reports
            DROP CONSTRAINT IF EXISTS scheduled_reports_delivery_claim_check;
        UPDATE scheduled_reports
        SET delivery_claim_token=NULL,delivery_claim_until=NULL
        WHERE (delivery_claim_token IS NULL) <> (delivery_claim_until IS NULL);
        ALTER TABLE scheduled_reports
            ADD CONSTRAINT scheduled_reports_delivery_claim_check CHECK(
                (delivery_claim_token IS NULL) = (delivery_claim_until IS NULL)
            );
        CREATE INDEX IF NOT EXISTS idx_scheduled_reports_claim
            ON scheduled_reports(delivery_claim_until)
            WHERE delivery_claim_until IS NOT NULL;
        """
    )


def _create_quickbooks_export_mappings(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE organization_settings
            ADD COLUMN IF NOT EXISTS quickbooks_company_name TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS quickbooks_company_create_time TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS quickbooks_default_service_item TEXT NOT NULL DEFAULT '';
        ALTER TABLE users
            ADD COLUMN IF NOT EXISTS quickbooks_name TEXT NOT NULL DEFAULT '';
        ALTER TABLE projects
            ADD COLUMN IF NOT EXISTS quickbooks_customer_job TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS quickbooks_class TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS quickbooks_billable BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE tasks
            ADD COLUMN IF NOT EXISTS quickbooks_service_item TEXT NOT NULL DEFAULT '';
        ALTER TABLE organization_settings
            DROP CONSTRAINT IF EXISTS organization_quickbooks_company_name_check,
            DROP CONSTRAINT IF EXISTS organization_quickbooks_company_time_check,
            DROP CONSTRAINT IF EXISTS organization_quickbooks_service_item_check;
        ALTER TABLE organization_settings
            ADD CONSTRAINT organization_quickbooks_company_name_check
                CHECK(length(quickbooks_company_name) <= 255),
            ADD CONSTRAINT organization_quickbooks_company_time_check CHECK(
                quickbooks_company_create_time = '' OR (
                    quickbooks_company_create_time ~ '^[0-9]{1,20}$'
                )
            ),
            ADD CONSTRAINT organization_quickbooks_service_item_check
                CHECK(length(quickbooks_default_service_item) <= 209);
        ALTER TABLE users
            DROP CONSTRAINT IF EXISTS users_quickbooks_name_check;
        ALTER TABLE users
            ADD CONSTRAINT users_quickbooks_name_check
                CHECK(length(quickbooks_name) <= 209);
        ALTER TABLE projects
            DROP CONSTRAINT IF EXISTS projects_quickbooks_customer_job_check,
            DROP CONSTRAINT IF EXISTS projects_quickbooks_class_check;
        ALTER TABLE projects
            ADD CONSTRAINT projects_quickbooks_customer_job_check
                CHECK(length(quickbooks_customer_job) <= 209),
            ADD CONSTRAINT projects_quickbooks_class_check
                CHECK(length(quickbooks_class) <= 159);
        ALTER TABLE tasks
            DROP CONSTRAINT IF EXISTS tasks_quickbooks_service_item_check;
        ALTER TABLE tasks
            ADD CONSTRAINT tasks_quickbooks_service_item_check
                CHECK(length(quickbooks_service_item) <= 209);
        """
    )


def _add_quickbooks_timezone(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE organization_settings
            ADD COLUMN IF NOT EXISTS quickbooks_timezone TEXT NOT NULL DEFAULT 'UTC';
        ALTER TABLE organization_settings
            DROP CONSTRAINT IF EXISTS organization_quickbooks_timezone_check;
        ALTER TABLE organization_settings
            ADD CONSTRAINT organization_quickbooks_timezone_check
                CHECK(length(quickbooks_timezone) BETWEEN 1 AND 80);
        """
    )


def _create_github_app_integration(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE integrations
            ADD COLUMN IF NOT EXISTS provider_external_id BIGINT,
            ADD COLUMN IF NOT EXISTS account_login TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS account_type TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS last_sync_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS next_sync_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            ADD COLUMN IF NOT EXISTS last_error_code TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS consecutive_failures INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS sync_claim_token UUID,
            ADD COLUMN IF NOT EXISTS sync_claim_until TIMESTAMPTZ;
        ALTER TABLE integrations
            DROP CONSTRAINT IF EXISTS integrations_provider_external_id_check,
            DROP CONSTRAINT IF EXISTS integrations_account_login_check,
            DROP CONSTRAINT IF EXISTS integrations_failure_count_check;
        ALTER TABLE integrations
            ADD CONSTRAINT integrations_provider_external_id_check
                CHECK(provider_external_id IS NULL OR provider_external_id > 0),
            ADD CONSTRAINT integrations_account_login_check
                CHECK(length(account_login) <= 255 AND length(account_type) <= 40),
            ADD CONSTRAINT integrations_failure_count_check
                CHECK(consecutive_failures BETWEEN 0 AND 1000000);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_integrations_github_installation
            ON integrations(provider,provider_external_id)
            WHERE provider='github' AND provider_external_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_integrations_due_sync
            ON integrations(next_sync_at)
            WHERE provider='github' AND enabled=TRUE;

        CREATE TABLE IF NOT EXISTS integration_project_mappings (
            integration_id UUID NOT NULL REFERENCES integrations(id) ON DELETE CASCADE,
            external_repository_id BIGINT NOT NULL CHECK(external_repository_id > 0),
            external_repository_name TEXT NOT NULL CHECK(
                length(external_repository_name) BETWEEN 1 AND 255
            ),
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(integration_id,external_repository_id)
        );
        CREATE INDEX IF NOT EXISTS idx_integration_mappings_project
            ON integration_project_mappings(project_id);

        ALTER TABLE tasks
            ADD COLUMN IF NOT EXISTS integration_id UUID REFERENCES integrations(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS external_repository_id BIGINT,
            ADD COLUMN IF NOT EXISTS external_key TEXT,
            ADD COLUMN IF NOT EXISTS external_url TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS external_updated_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS external_read_only BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE tasks
            DROP CONSTRAINT IF EXISTS tasks_external_link_check;
        ALTER TABLE tasks
            ADD CONSTRAINT tasks_external_link_check CHECK(
                (integration_id IS NULL AND external_key IS NULL
                 AND external_repository_id IS NULL AND external_read_only=FALSE)
                OR
                (integration_id IS NOT NULL AND external_key IS NOT NULL
                 AND external_repository_id IS NOT NULL AND external_repository_id > 0
                 AND external_read_only=TRUE AND length(external_key) <= 500
                 AND length(external_url) <= 1000)
            );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_integration_external_key
            ON tasks(integration_id,external_key)
            WHERE integration_id IS NOT NULL AND external_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_tasks_external_repository
            ON tasks(integration_id,external_repository_id)
            WHERE integration_id IS NOT NULL;

        CREATE TABLE IF NOT EXISTS integration_webhook_deliveries (
            provider TEXT NOT NULL,
            delivery_id TEXT NOT NULL CHECK(length(delivery_id) BETWEEN 1 AND 120),
            event_name TEXT NOT NULL CHECK(length(event_name) BETWEEN 1 AND 80),
            received_at TIMESTAMPTZ NOT NULL,
            processed_at TIMESTAMPTZ,
            outcome TEXT NOT NULL DEFAULT 'received' CHECK(
                outcome IN ('received','processed','ignored','failed')
            ),
            PRIMARY KEY(provider,delivery_id)
        );
        CREATE INDEX IF NOT EXISTS idx_integration_webhook_received
            ON integration_webhook_deliveries(received_at);
        """
    )


def _add_github_incremental_sync_cursors(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE integration_project_mappings
            ADD COLUMN IF NOT EXISTS last_full_sync_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS last_incremental_sync_at TIMESTAMPTZ;
        """
    )


def _protect_github_full_reconciliation(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE tasks
            ADD COLUMN IF NOT EXISTS external_observed_at TIMESTAMPTZ;
        """
    )


def _create_integration_credential_vault(connection: Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS integration_credentials (
            integration_id UUID PRIMARY KEY
                REFERENCES integrations(id) ON DELETE CASCADE,
            key_id TEXT NOT NULL CHECK(
                key_id ~ '^[A-Za-z0-9._-]{1,32}$'
            ),
            ciphertext BYTEA NOT NULL CHECK(
                octet_length(ciphertext) BETWEEN 20 AND 131072
            ),
            revision BIGINT NOT NULL DEFAULT 1 CHECK(revision > 0),
            access_expires_at TIMESTAMPTZ,
            refresh_claim_token UUID,
            refresh_claim_until TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            CHECK(
                (refresh_claim_token IS NULL AND refresh_claim_until IS NULL)
                OR
                (refresh_claim_token IS NOT NULL AND refresh_claim_until IS NOT NULL)
            )
        );
        CREATE INDEX IF NOT EXISTS idx_integration_credentials_refresh_claim
            ON integration_credentials(refresh_claim_until)
            WHERE refresh_claim_until IS NOT NULL;
        """
    )


def _create_jira_cloud_integration(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE integrations
            ADD COLUMN IF NOT EXISTS provider_resource_key TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS provider_resource_url TEXT NOT NULL DEFAULT '';
        ALTER TABLE integrations
            DROP CONSTRAINT IF EXISTS integrations_provider_resource_check;
        ALTER TABLE integrations
            ADD CONSTRAINT integrations_provider_resource_check CHECK(
                length(provider_resource_key) <= 255
                AND length(provider_resource_url) <= 1000
            );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_integrations_jira_site
            ON integrations(provider,provider_resource_key)
            WHERE provider='jira' AND provider_resource_key<>'';
        CREATE INDEX IF NOT EXISTS idx_integrations_jira_due_sync
            ON integrations(next_sync_at)
            WHERE provider='jira' AND enabled=TRUE;

        CREATE TABLE IF NOT EXISTS jira_project_mappings (
            integration_id UUID NOT NULL
                REFERENCES integrations(id) ON DELETE CASCADE,
            external_project_id TEXT NOT NULL CHECK(
                external_project_id ~ '^[A-Za-z0-9_-]{1,255}$'
            ),
            external_project_key TEXT NOT NULL CHECK(
                external_project_key ~ '^[A-Za-z][A-Za-z0-9_]{0,49}$'
            ),
            external_project_name TEXT NOT NULL CHECK(
                length(external_project_name) BETWEEN 1 AND 255
            ),
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            last_full_sync_at TIMESTAMPTZ,
            last_incremental_sync_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(integration_id,external_project_id)
        );
        CREATE INDEX IF NOT EXISTS idx_jira_mappings_project
            ON jira_project_mappings(project_id);

        ALTER TABLE tasks
            ADD COLUMN IF NOT EXISTS external_container_key TEXT;
        ALTER TABLE tasks
            DROP CONSTRAINT IF EXISTS tasks_external_link_check;
        ALTER TABLE tasks
            ADD CONSTRAINT tasks_external_link_check CHECK(
                (integration_id IS NULL AND external_key IS NULL
                 AND external_repository_id IS NULL
                 AND external_container_key IS NULL
                 AND external_read_only=FALSE)
                OR
                (integration_id IS NOT NULL AND external_key IS NOT NULL
                 AND external_read_only=TRUE AND length(external_key) <= 500
                 AND length(external_url) <= 1000
                 AND (
                     (external_repository_id IS NOT NULL
                      AND external_repository_id > 0
                      AND external_container_key IS NULL)
                     OR
                     (external_repository_id IS NULL
                      AND length(external_container_key) BETWEEN 1 AND 255)
                 ))
            );
        CREATE INDEX IF NOT EXISTS idx_tasks_external_container
            ON tasks(integration_id,external_container_key)
            WHERE integration_id IS NOT NULL AND external_container_key IS NOT NULL;
        """
    )


def _protect_jira_eventual_reconciliation(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE tasks
            ADD COLUMN IF NOT EXISTS external_missing_since TIMESTAMPTZ;
        """
    )


def _retain_tasks_after_integration_disconnect(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE tasks
            DROP CONSTRAINT IF EXISTS tasks_external_link_check;
        ALTER TABLE tasks
            ADD CONSTRAINT tasks_external_link_check CHECK(
                (integration_id IS NULL AND external_key IS NULL
                 AND external_repository_id IS NULL
                 AND external_container_key IS NULL
                 AND external_read_only=FALSE)
                OR
                (integration_id IS NOT NULL AND external_key IS NOT NULL
                 AND length(external_key) <= 500
                 AND length(external_url) <= 1000
                 AND (
                     (external_repository_id IS NOT NULL
                      AND external_repository_id > 0
                      AND external_container_key IS NULL)
                     OR
                     (external_repository_id IS NULL
                      AND length(external_container_key) BETWEEN 1 AND 255)
                 ))
            );
        """
    )


def _create_integration_user_credentials(connection: Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS integration_user_credentials (
            integration_id UUID NOT NULL
                REFERENCES integrations(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            key_id TEXT NOT NULL CHECK(
                key_id ~ '^[A-Za-z0-9._-]{1,32}$'
            ),
            ciphertext BYTEA NOT NULL CHECK(
                octet_length(ciphertext) BETWEEN 20 AND 131072
            ),
            revision BIGINT NOT NULL DEFAULT 1 CHECK(revision > 0),
            access_expires_at TIMESTAMPTZ,
            refresh_claim_token UUID,
            refresh_claim_until TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(integration_id,user_id),
            CHECK(
                (refresh_claim_token IS NULL AND refresh_claim_until IS NULL)
                OR
                (refresh_claim_token IS NOT NULL AND refresh_claim_until IS NOT NULL)
            )
        );
        CREATE INDEX IF NOT EXISTS idx_user_credentials_refresh_claim
            ON integration_user_credentials(refresh_claim_until)
            WHERE refresh_claim_until IS NOT NULL;

        CREATE TABLE IF NOT EXISTS jira_user_connections (
            integration_id UUID NOT NULL
                REFERENCES integrations(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            atlassian_account_id TEXT NOT NULL CHECK(
                length(atlassian_account_id) BETWEEN 1 AND 255
            ),
            display_name TEXT NOT NULL CHECK(length(display_name) BETWEEN 1 AND 255),
            credential_kind TEXT NOT NULL CHECK(
                credential_kind IN ('site','member')
            ),
            time_sync_mode TEXT NOT NULL DEFAULT 'daily' CHECK(
                time_sync_mode IN ('off','hourly','daily','delayed')
            ),
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            connected_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(integration_id,user_id),
            UNIQUE(integration_id,atlassian_account_id)
        );
        CREATE INDEX IF NOT EXISTS idx_jira_user_connections_user
            ON jira_user_connections(user_id,enabled);
        """
    )


def _create_jira_worklog_delivery(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE tasks
            ADD COLUMN IF NOT EXISTS external_display_key TEXT;
        UPDATE tasks t SET external_display_key = substring(
            t.name FROM '^([A-Za-z][A-Za-z0-9_]{0,49}-[1-9][0-9]{0,19}):'
        )
        FROM integrations i
        WHERE i.id=t.integration_id AND i.provider='jira'
          AND t.external_display_key IS NULL;
        ALTER TABLE tasks
            DROP CONSTRAINT IF EXISTS tasks_external_display_key_check;
        ALTER TABLE tasks
            ADD CONSTRAINT tasks_external_display_key_check CHECK(
                external_display_key IS NULL
                OR length(external_display_key) BETWEEN 1 AND 255
            );

        ALTER TABLE jira_user_connections
            ADD COLUMN IF NOT EXISTS next_worklog_sync_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS last_worklog_sync_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS last_worklog_error_code TEXT NOT NULL DEFAULT '';
        UPDATE jira_user_connections
        SET next_worklog_sync_at=COALESCE(next_worklog_sync_at,CURRENT_TIMESTAMP)
        WHERE enabled=TRUE;
        CREATE INDEX IF NOT EXISTS idx_jira_user_worklog_due
            ON jira_user_connections(next_worklog_sync_at)
            WHERE enabled=TRUE AND time_sync_mode<>'off';

        CREATE TABLE IF NOT EXISTS jira_worklog_dirty_days (
            integration_id UUID NOT NULL
                REFERENCES integrations(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            work_date DATE NOT NULL,
            changed_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(integration_id,user_id,task_id,work_date)
        );
        CREATE INDEX IF NOT EXISTS idx_jira_worklog_dirty_user
            ON jira_worklog_dirty_days(integration_id,user_id,work_date,changed_at);

        CREATE TABLE IF NOT EXISTS jira_worklog_exports (
            id UUID PRIMARY KEY,
            integration_id UUID NOT NULL
                REFERENCES integrations(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            work_date DATE NOT NULL,
            atlassian_account_id TEXT NOT NULL CHECK(
                length(atlassian_account_id) BETWEEN 1 AND 255
            ),
            desired_seconds BIGINT NOT NULL CHECK(
                desired_seconds BETWEEN 0 AND 2147483647
            ),
            desired_started_at TIMESTAMPTZ,
            synced_seconds BIGINT CHECK(
                synced_seconds IS NULL
                OR synced_seconds BETWEEN 0 AND 2147483647
            ),
            synced_started_at TIMESTAMPTZ,
            provider_worklog_id TEXT CHECK(
                provider_worklog_id IS NULL
                OR provider_worklog_id ~ '^[1-9][0-9]{0,19}$'
            ),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
            next_attempt_at TIMESTAMPTZ NOT NULL,
            claim_token UUID,
            claim_until TIMESTAMPTZ,
            last_error_code TEXT NOT NULL DEFAULT '' CHECK(
                length(last_error_code) <= 64
            ),
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            synced_at TIMESTAMPTZ,
            UNIQUE(integration_id,user_id,task_id,work_date),
            CHECK(
                (desired_seconds=0 AND desired_started_at IS NULL)
                OR (desired_seconds>0 AND desired_started_at IS NOT NULL)
            ),
            CHECK(
                (claim_token IS NULL AND claim_until IS NULL)
                OR (claim_token IS NOT NULL AND claim_until IS NOT NULL)
            )
        );
        CREATE INDEX IF NOT EXISTS idx_jira_worklog_exports_due
            ON jira_worklog_exports(next_attempt_at,claim_until,updated_at);

        CREATE OR REPLACE FUNCTION dayfinch_enqueue_jira_worklog_days(
            p_user_id UUID,
            p_task_id UUID,
            p_started_at TIMESTAMPTZ,
            p_ended_at TIMESTAMPTZ
        ) RETURNS VOID LANGUAGE plpgsql AS $$
        DECLARE
            provider_integration_id UUID;
        BEGIN
            IF p_user_id IS NULL OR p_task_id IS NULL OR p_ended_at IS NULL
               OR p_ended_at <= p_started_at THEN
                RETURN;
            END IF;
            SELECT t.integration_id INTO provider_integration_id
            FROM tasks t JOIN integrations i ON i.id=t.integration_id
            WHERE t.id=p_task_id AND i.provider='jira';
            IF provider_integration_id IS NULL THEN
                RETURN;
            END IF;
            INSERT INTO jira_worklog_dirty_days(
                integration_id,user_id,task_id,work_date,changed_at
            )
            SELECT provider_integration_id,p_user_id,p_task_id,day_value::date,
                   clock_timestamp()
            FROM generate_series(
                date_trunc('day',p_started_at),
                date_trunc('day',p_ended_at-INTERVAL '1 microsecond'),
                INTERVAL '1 day'
            ) day_value
            ON CONFLICT(integration_id,user_id,task_id,work_date)
            DO UPDATE SET changed_at=EXCLUDED.changed_at;
        END $$;

        CREATE OR REPLACE FUNCTION dayfinch_jira_segment_dirty_trigger()
        RETURNS TRIGGER LANGUAGE plpgsql AS $$
        DECLARE session_row RECORD;
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                SELECT user_id,task_id INTO session_row
                FROM work_sessions WHERE id=OLD.session_id;
                PERFORM dayfinch_enqueue_jira_worklog_days(
                    session_row.user_id,session_row.task_id,
                    OLD.started_at,OLD.ended_at
                );
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                SELECT user_id,task_id INTO session_row
                FROM work_sessions WHERE id=NEW.session_id;
                PERFORM dayfinch_enqueue_jira_worklog_days(
                    session_row.user_id,session_row.task_id,
                    NEW.started_at,NEW.ended_at
                );
            END IF;
            IF TG_OP='DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END $$;
        DROP TRIGGER IF EXISTS trg_jira_segment_dirty ON work_session_segments;
        CREATE TRIGGER trg_jira_segment_dirty
        BEFORE INSERT OR UPDATE OR DELETE ON work_session_segments
        FOR EACH ROW EXECUTE FUNCTION dayfinch_jira_segment_dirty_trigger();

        CREATE OR REPLACE FUNCTION dayfinch_jira_manual_dirty_trigger()
        RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN
                PERFORM dayfinch_enqueue_jira_worklog_days(
                    OLD.user_id,OLD.task_id,OLD.started_at,
                    CASE WHEN OLD.status='approved' THEN OLD.ended_at ELSE NULL END
                );
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN
                PERFORM dayfinch_enqueue_jira_worklog_days(
                    NEW.user_id,NEW.task_id,NEW.started_at,
                    CASE WHEN NEW.status='approved' THEN NEW.ended_at ELSE NULL END
                );
            END IF;
            IF TG_OP='DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END $$;
        DROP TRIGGER IF EXISTS trg_jira_manual_dirty ON manual_time_entries;
        CREATE TRIGGER trg_jira_manual_dirty
        BEFORE INSERT OR UPDATE OR DELETE ON manual_time_entries
        FOR EACH ROW EXECUTE FUNCTION dayfinch_jira_manual_dirty_trigger();

        INSERT INTO jira_worklog_dirty_days(
            integration_id,user_id,task_id,work_date,changed_at
        )
        SELECT t.integration_id,ws.user_id,ws.task_id,day_value::date,CURRENT_TIMESTAMP
        FROM work_session_segments seg
        JOIN work_sessions ws ON ws.id=seg.session_id
        JOIN tasks t ON t.id=ws.task_id
        JOIN integrations i ON i.id=t.integration_id AND i.provider='jira'
        JOIN jira_user_connections c
          ON c.integration_id=i.id AND c.user_id=ws.user_id
        CROSS JOIN LATERAL generate_series(
            date_trunc('day',GREATEST(seg.started_at,c.connected_at)),
            date_trunc('day',seg.ended_at-INTERVAL '1 microsecond'),
            INTERVAL '1 day'
        ) day_value
        WHERE seg.ended_at IS NOT NULL AND seg.ended_at>c.connected_at
        ON CONFLICT(integration_id,user_id,task_id,work_date)
        DO UPDATE SET changed_at=EXCLUDED.changed_at;

        INSERT INTO jira_worklog_dirty_days(
            integration_id,user_id,task_id,work_date,changed_at
        )
        SELECT t.integration_id,m.user_id,m.task_id,day_value::date,CURRENT_TIMESTAMP
        FROM manual_time_entries m
        JOIN tasks t ON t.id=m.task_id
        JOIN integrations i ON i.id=t.integration_id AND i.provider='jira'
        JOIN jira_user_connections c
          ON c.integration_id=i.id AND c.user_id=m.user_id
        CROSS JOIN LATERAL generate_series(
            date_trunc('day',GREATEST(m.started_at,c.connected_at)),
            date_trunc('day',m.ended_at-INTERVAL '1 microsecond'),
            INTERVAL '1 day'
        ) day_value
        WHERE m.status='approved' AND m.ended_at>c.connected_at
        ON CONFLICT(integration_id,user_id,task_id,work_date)
        DO UPDATE SET changed_at=EXCLUDED.changed_at;
        """
    )


def _create_jira_authorization_periods(connection: Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS jira_user_authorization_periods (
            id UUID PRIMARY KEY,
            integration_id UUID NOT NULL
                REFERENCES integrations(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            atlassian_account_id TEXT NOT NULL CHECK(
                length(atlassian_account_id) BETWEEN 1 AND 255
            ),
            started_at TIMESTAMPTZ NOT NULL,
            ended_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL,
            CHECK(ended_at IS NULL OR ended_at>=started_at)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_jira_authorization_open_period
            ON jira_user_authorization_periods(integration_id,user_id)
            WHERE ended_at IS NULL;
        CREATE INDEX IF NOT EXISTS idx_jira_authorization_period_lookup
            ON jira_user_authorization_periods(
                integration_id,user_id,atlassian_account_id,started_at,ended_at
            );
        INSERT INTO jira_user_authorization_periods(
            id,integration_id,user_id,atlassian_account_id,started_at,created_at
        )
        SELECT gen_random_uuid(),c.integration_id,c.user_id,
               c.atlassian_account_id,c.connected_at,CURRENT_TIMESTAMP
        FROM jira_user_connections c
        WHERE c.enabled=TRUE AND NOT EXISTS(
            SELECT 1 FROM jira_user_authorization_periods p
            WHERE p.integration_id=c.integration_id AND p.user_id=c.user_id
              AND p.ended_at IS NULL
        );
        """
    )


def _create_asana_cloud_integration(connection: Connection) -> None:
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_integrations_asana_workspace
            ON integrations(provider,provider_resource_key)
            WHERE provider='asana' AND provider_resource_key<>'';
        CREATE INDEX IF NOT EXISTS idx_integrations_asana_due_sync
            ON integrations(next_sync_at)
            WHERE provider='asana' AND enabled=TRUE;

        CREATE TABLE IF NOT EXISTS asana_project_mappings (
            integration_id UUID NOT NULL
                REFERENCES integrations(id) ON DELETE CASCADE,
            external_project_id TEXT NOT NULL CHECK(
                external_project_id ~ '^[^[:space:][:cntrl:]/]{1,200}$'
            ),
            external_project_name TEXT NOT NULL CHECK(
                length(external_project_name) BETWEEN 1 AND 255
            ),
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            last_full_sync_at TIMESTAMPTZ,
            sync_claim_token UUID,
            sync_claim_until TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(integration_id,external_project_id),
            CHECK(
                (sync_claim_token IS NULL AND sync_claim_until IS NULL)
                OR
                (sync_claim_token IS NOT NULL AND sync_claim_until IS NOT NULL)
            )
        );
        CREATE INDEX IF NOT EXISTS idx_asana_mappings_project
            ON asana_project_mappings(project_id);
        CREATE INDEX IF NOT EXISTS idx_asana_mappings_due
            ON asana_project_mappings(last_full_sync_at,sync_claim_until,updated_at);

        CREATE TABLE IF NOT EXISTS asana_user_connections (
            integration_id UUID NOT NULL
                REFERENCES integrations(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            asana_user_gid TEXT NOT NULL CHECK(
                asana_user_gid ~ '^[^[:space:][:cntrl:]/]{1,200}$'
            ),
            display_name TEXT NOT NULL CHECK(length(display_name) BETWEEN 1 AND 255),
            credential_kind TEXT NOT NULL CHECK(credential_kind IN ('site','member')),
            time_sync_mode TEXT NOT NULL DEFAULT 'off' CHECK(
                time_sync_mode IN ('off','hourly','daily','delayed','completed')
            ),
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            connected_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(integration_id,user_id),
            UNIQUE(integration_id,asana_user_gid)
        );
        CREATE INDEX IF NOT EXISTS idx_asana_user_connections_user
            ON asana_user_connections(user_id,enabled);

        CREATE TABLE IF NOT EXISTS asana_task_assignees (
            integration_id UUID NOT NULL
                REFERENCES integrations(id) ON DELETE CASCADE,
            external_project_id TEXT NOT NULL,
            external_task_gid TEXT NOT NULL CHECK(
                external_task_gid ~ '^[^[:space:][:cntrl:]/]{1,200}$'
            ),
            asana_user_gid TEXT NOT NULL CHECK(
                asana_user_gid = ''
                OR asana_user_gid ~ '^[^[:space:][:cntrl:]/]{1,200}$'
            ),
            observed_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(integration_id,external_project_id,external_task_gid),
            FOREIGN KEY(integration_id,external_project_id)
                REFERENCES asana_project_mappings(integration_id,external_project_id)
                ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_asana_task_assignees_lookup
            ON asana_task_assignees(integration_id,external_task_gid,asana_user_gid);

        CREATE TABLE IF NOT EXISTS integration_oauth_pending (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider TEXT NOT NULL CHECK(provider ~ '^[a-z][a-z0-9_-]{0,59}$'),
            key_id TEXT NOT NULL CHECK(key_id ~ '^[A-Za-z0-9._-]{1,32}$'),
            ciphertext BYTEA NOT NULL CHECK(
                octet_length(ciphertext) BETWEEN 20 AND 131072
            ),
            expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_integration_oauth_pending_expiry
            ON integration_oauth_pending(expires_at);

        CREATE INDEX IF NOT EXISTS idx_tasks_asana_object
            ON tasks(integration_id,external_display_key,external_container_key)
            WHERE integration_id IS NOT NULL AND external_display_key IS NOT NULL;
        """
    )


def _create_asana_time_comment_delivery(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE asana_user_connections
            ADD COLUMN IF NOT EXISTS next_comment_sync_at TIMESTAMPTZ
                NOT NULL DEFAULT CURRENT_TIMESTAMP,
            ADD COLUMN IF NOT EXISTS last_comment_sync_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS last_comment_error_code TEXT NOT NULL DEFAULT '';
        CREATE INDEX IF NOT EXISTS idx_asana_user_comment_due
            ON asana_user_connections(next_comment_sync_at)
            WHERE enabled=TRUE AND time_sync_mode<>'off';

        CREATE TABLE IF NOT EXISTS asana_user_authorization_periods (
            id UUID PRIMARY KEY,
            integration_id UUID NOT NULL REFERENCES integrations(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            asana_user_gid TEXT NOT NULL CHECK(length(asana_user_gid) BETWEEN 1 AND 200),
            started_at TIMESTAMPTZ NOT NULL,
            ended_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL,
            CHECK(ended_at IS NULL OR ended_at>=started_at)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_asana_authorization_open_period
            ON asana_user_authorization_periods(integration_id,user_id)
            WHERE ended_at IS NULL;
        CREATE INDEX IF NOT EXISTS idx_asana_authorization_period_lookup
            ON asana_user_authorization_periods(
                integration_id,user_id,asana_user_gid,started_at,ended_at
            );

        CREATE TABLE IF NOT EXISTS asana_comment_dirty_days (
            integration_id UUID NOT NULL REFERENCES integrations(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            external_task_gid TEXT NOT NULL CHECK(length(external_task_gid) BETWEEN 1 AND 200),
            work_date DATE NOT NULL,
            changed_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(integration_id,user_id,external_task_gid,work_date)
        );
        CREATE INDEX IF NOT EXISTS idx_asana_comment_dirty_user
            ON asana_comment_dirty_days(integration_id,user_id,work_date,changed_at);

        CREATE TABLE IF NOT EXISTS asana_comment_exports (
            id UUID PRIMARY KEY,
            integration_id UUID NOT NULL REFERENCES integrations(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            external_task_gid TEXT NOT NULL CHECK(length(external_task_gid) BETWEEN 1 AND 200),
            work_date DATE NOT NULL,
            asana_user_gid TEXT NOT NULL CHECK(length(asana_user_gid) BETWEEN 1 AND 200),
            desired_seconds INTEGER NOT NULL DEFAULT 0 CHECK(desired_seconds>=0),
            desired_started_at TIMESTAMPTZ,
            synced_seconds INTEGER NOT NULL DEFAULT 0 CHECK(synced_seconds>=0),
            synced_started_at TIMESTAMPTZ,
            provider_story_gid TEXT CHECK(
                provider_story_gid IS NULL OR length(provider_story_gid) BETWEEN 1 AND 200
            ),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
            next_attempt_at TIMESTAMPTZ NOT NULL,
            claim_token UUID,
            claim_until TIMESTAMPTZ,
            last_error_code TEXT NOT NULL DEFAULT '',
            synced_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            UNIQUE(integration_id,user_id,external_task_gid,work_date),
            CHECK(
                (claim_token IS NULL AND claim_until IS NULL)
                OR (claim_token IS NOT NULL AND claim_until IS NOT NULL)
            ),
            CHECK(
                (desired_seconds=0 AND desired_started_at IS NULL)
                OR (desired_seconds>0 AND desired_started_at IS NOT NULL)
            ),
            CHECK(
                (synced_seconds=0 AND synced_started_at IS NULL)
                OR (synced_seconds>0 AND synced_started_at IS NOT NULL)
            )
        );
        CREATE INDEX IF NOT EXISTS idx_asana_comment_exports_due
            ON asana_comment_exports(next_attempt_at,claim_until,updated_at);

        CREATE OR REPLACE FUNCTION dayfinch_enqueue_asana_comment_days(
            p_user_id UUID,p_task_id UUID,p_started_at TIMESTAMPTZ,
            p_ended_at TIMESTAMPTZ
        ) RETURNS VOID AS $$
        DECLARE provider_integration_id UUID;
        DECLARE provider_task_gid TEXT;
        BEGIN
            IF p_task_id IS NULL OR p_started_at IS NULL OR p_ended_at IS NULL
               OR p_ended_at<=p_started_at THEN RETURN; END IF;
            SELECT t.integration_id,t.external_display_key
            INTO provider_integration_id,provider_task_gid
            FROM tasks t JOIN integrations i ON i.id=t.integration_id
            WHERE t.id=p_task_id AND i.provider='asana';
            IF provider_integration_id IS NULL OR provider_task_gid IS NULL THEN RETURN; END IF;
            INSERT INTO asana_comment_dirty_days(
                integration_id,user_id,external_task_gid,work_date,changed_at
            )
            SELECT provider_integration_id,p_user_id,provider_task_gid,
                   day_value::date,CURRENT_TIMESTAMP
            FROM generate_series(
                date_trunc('day',p_started_at),
                date_trunc('day',p_ended_at-INTERVAL '1 microsecond'),
                INTERVAL '1 day'
            ) day_value
            ON CONFLICT(integration_id,user_id,external_task_gid,work_date)
            DO UPDATE SET changed_at=EXCLUDED.changed_at;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION dayfinch_asana_segment_dirty_trigger()
        RETURNS TRIGGER AS $$
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') AND OLD.ended_at IS NOT NULL THEN
                PERFORM dayfinch_enqueue_asana_comment_days(
                    (SELECT user_id FROM work_sessions WHERE id=OLD.session_id),
                    (SELECT task_id FROM work_sessions WHERE id=OLD.session_id),
                    OLD.started_at,OLD.ended_at
                );
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') AND NEW.ended_at IS NOT NULL THEN
                PERFORM dayfinch_enqueue_asana_comment_days(
                    (SELECT user_id FROM work_sessions WHERE id=NEW.session_id),
                    (SELECT task_id FROM work_sessions WHERE id=NEW.session_id),
                    NEW.started_at,NEW.ended_at
                );
            END IF;
            RETURN COALESCE(NEW,OLD);
        END;
        $$ LANGUAGE plpgsql;
        DROP TRIGGER IF EXISTS trg_asana_segment_dirty ON work_session_segments;
        CREATE TRIGGER trg_asana_segment_dirty
        AFTER INSERT OR UPDATE OR DELETE ON work_session_segments
        FOR EACH ROW EXECUTE FUNCTION dayfinch_asana_segment_dirty_trigger();

        CREATE OR REPLACE FUNCTION dayfinch_asana_manual_dirty_trigger()
        RETURNS TRIGGER AS $$
        BEGIN
            IF TG_OP IN ('UPDATE','DELETE') AND OLD.status='approved' THEN
                PERFORM dayfinch_enqueue_asana_comment_days(
                    OLD.user_id,OLD.task_id,OLD.started_at,OLD.ended_at
                );
            END IF;
            IF TG_OP IN ('INSERT','UPDATE') AND NEW.status='approved' THEN
                PERFORM dayfinch_enqueue_asana_comment_days(
                    NEW.user_id,NEW.task_id,NEW.started_at,NEW.ended_at
                );
            END IF;
            RETURN COALESCE(NEW,OLD);
        END;
        $$ LANGUAGE plpgsql;
        DROP TRIGGER IF EXISTS trg_asana_manual_dirty ON manual_time_entries;
        CREATE TRIGGER trg_asana_manual_dirty
        AFTER INSERT OR UPDATE OR DELETE ON manual_time_entries
        FOR EACH ROW EXECUTE FUNCTION dayfinch_asana_manual_dirty_trigger();

        INSERT INTO asana_user_authorization_periods(
            id,integration_id,user_id,asana_user_gid,started_at,created_at
        )
        SELECT gen_random_uuid(),c.integration_id,c.user_id,c.asana_user_gid,
               c.connected_at,CURRENT_TIMESTAMP
        FROM asana_user_connections c
        WHERE c.enabled=TRUE AND NOT EXISTS(
            SELECT 1 FROM asana_user_authorization_periods p
            WHERE p.integration_id=c.integration_id AND p.user_id=c.user_id
              AND p.ended_at IS NULL
        );

        INSERT INTO asana_comment_dirty_days(
            integration_id,user_id,external_task_gid,work_date,changed_at
        )
        SELECT DISTINCT t.integration_id,ws.user_id,t.external_display_key,
               day_value::date,CURRENT_TIMESTAMP
        FROM work_sessions ws
        JOIN work_session_segments s ON s.session_id=ws.id AND s.ended_at IS NOT NULL
        JOIN tasks t ON t.id=ws.task_id AND t.external_display_key IS NOT NULL
        JOIN integrations i ON i.id=t.integration_id AND i.provider='asana'
        JOIN asana_user_connections c
          ON c.integration_id=i.id AND c.user_id=ws.user_id AND c.enabled=TRUE
        CROSS JOIN LATERAL generate_series(
            date_trunc('day',GREATEST(s.started_at,c.connected_at)),
            date_trunc('day',s.ended_at-INTERVAL '1 microsecond'),
            INTERVAL '1 day'
        ) day_value
        WHERE s.ended_at>c.connected_at
        ON CONFLICT(integration_id,user_id,external_task_gid,work_date)
        DO UPDATE SET changed_at=EXCLUDED.changed_at;

        INSERT INTO asana_comment_dirty_days(
            integration_id,user_id,external_task_gid,work_date,changed_at
        )
        SELECT DISTINCT t.integration_id,m.user_id,t.external_display_key,
               day_value::date,CURRENT_TIMESTAMP
        FROM manual_time_entries m
        JOIN tasks t ON t.id=m.task_id AND t.external_display_key IS NOT NULL
        JOIN integrations i ON i.id=t.integration_id AND i.provider='asana'
        JOIN asana_user_connections c
          ON c.integration_id=i.id AND c.user_id=m.user_id AND c.enabled=TRUE
        CROSS JOIN LATERAL generate_series(
            date_trunc('day',GREATEST(m.started_at,c.connected_at)),
            date_trunc('day',m.ended_at-INTERVAL '1 microsecond'),
            INTERVAL '1 day'
        ) day_value
        WHERE m.status='approved' AND m.ended_at>c.connected_at
        ON CONFLICT(integration_id,user_id,external_task_gid,work_date)
        DO UPDATE SET changed_at=EXCLUDED.changed_at;
        """
    )


def _create_slack_notifications(connection: Connection) -> None:
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_integrations_slack_workspace
            ON integrations(provider,provider_resource_key)
            WHERE provider='slack' AND provider_resource_key<>'';

        CREATE TABLE IF NOT EXISTS slack_notification_defaults (
            integration_id UUID PRIMARY KEY
                REFERENCES integrations(id) ON DELETE CASCADE,
            timer_events BOOLEAN NOT NULL DEFAULT TRUE,
            todo_events BOOLEAN NOT NULL DEFAULT TRUE,
            updated_at TIMESTAMPTZ NOT NULL
        );

        CREATE TABLE IF NOT EXISTS slack_notification_users (
            integration_id UUID NOT NULL
                REFERENCES integrations(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            timer_events BOOLEAN,
            todo_events BOOLEAN,
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(integration_id,user_id),
            CHECK(timer_events IS NOT NULL OR todo_events IS NOT NULL)
        );

        CREATE TABLE IF NOT EXISTS slack_destinations (
            id UUID PRIMARY KEY,
            integration_id UUID NOT NULL
                REFERENCES integrations(id) ON DELETE CASCADE,
            slack_target_id TEXT NOT NULL CHECK(
                slack_target_id ~ '^[A-Z][A-Z0-9]{1,30}$'
            ),
            target_kind TEXT NOT NULL CHECK(target_kind IN ('channel','user')),
            display_name TEXT NOT NULL CHECK(length(display_name) BETWEEN 1 AND 255),
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            UNIQUE(integration_id,slack_target_id)
        );
        CREATE INDEX IF NOT EXISTS idx_slack_destinations_integration
            ON slack_destinations(integration_id,enabled,display_name);

        CREATE TABLE IF NOT EXISTS slack_outbox (
            id UUID PRIMARY KEY,
            integration_id UUID NOT NULL
                REFERENCES integrations(id) ON DELETE CASCADE,
            destination_id UUID NOT NULL
                REFERENCES slack_destinations(id) ON DELETE CASCADE,
            event_key TEXT NOT NULL CHECK(length(event_key) BETWEEN 1 AND 500),
            event_type TEXT NOT NULL CHECK(
                event_type IN ('timer_started','timer_stopped','todo_completed')
            ),
            user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            project_id UUID REFERENCES projects(id) ON DELETE SET NULL,
            task_id UUID REFERENCES tasks(id) ON DELETE SET NULL,
            session_id UUID REFERENCES work_sessions(id) ON DELETE SET NULL,
            todo_id UUID REFERENCES global_todos(id) ON DELETE SET NULL,
            occurred_at TIMESTAMPTZ NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
            next_attempt_at TIMESTAMPTZ NOT NULL,
            claim_token UUID,
            claim_until TIMESTAMPTZ,
            provider_ts TEXT CHECK(provider_ts IS NULL OR length(provider_ts)<=64),
            last_error_code TEXT NOT NULL DEFAULT '' CHECK(
                length(last_error_code)<=64
            ),
            sent_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            UNIQUE(destination_id,event_key),
            CHECK(
                (claim_token IS NULL AND claim_until IS NULL)
                OR (claim_token IS NOT NULL AND claim_until IS NOT NULL)
            )
        );
        CREATE INDEX IF NOT EXISTS idx_slack_outbox_due
            ON slack_outbox(next_attempt_at,claim_until,created_at)
            WHERE sent_at IS NULL;

        CREATE OR REPLACE FUNCTION dayfinch_enqueue_slack_session_notification()
        RETURNS TRIGGER AS $$
        DECLARE notification_type TEXT;
        DECLARE notification_key TEXT;
        DECLARE notification_time TIMESTAMPTZ;
        BEGIN
            IF TG_OP='INSERT' AND NEW.status IN ('active','paused') THEN
                notification_type := 'timer_started';
                notification_key := 'session:' || NEW.id::text || ':started';
                notification_time := NEW.started_at;
            ELSIF TG_OP='UPDATE' AND OLD.status<>'stopped'
                  AND NEW.status='stopped' THEN
                notification_type := 'timer_stopped';
                notification_key := 'session:' || NEW.id::text || ':stopped';
                notification_time := COALESCE(NEW.ended_at,NEW.updated_at,CURRENT_TIMESTAMP);
            ELSE
                RETURN NEW;
            END IF;
            INSERT INTO slack_outbox(
                id,integration_id,destination_id,event_key,event_type,user_id,
                project_id,task_id,session_id,occurred_at,next_attempt_at,
                created_at,updated_at
            )
            SELECT gen_random_uuid(),i.id,d.id,notification_key,notification_type,
                   NEW.user_id,NEW.project_id,NEW.task_id,NEW.id,notification_time,
                   CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP
            FROM integrations i
            JOIN slack_notification_defaults defaults
              ON defaults.integration_id=i.id
            JOIN slack_destinations d
              ON d.integration_id=i.id AND d.enabled=TRUE
            LEFT JOIN slack_notification_users member_rule
              ON member_rule.integration_id=i.id AND member_rule.user_id=NEW.user_id
            WHERE i.provider='slack' AND i.enabled=TRUE
              AND COALESCE(member_rule.timer_events,defaults.timer_events)=TRUE
            ON CONFLICT(destination_id,event_key) DO NOTHING;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        DROP TRIGGER IF EXISTS trg_slack_session_notification ON work_sessions;
        CREATE TRIGGER trg_slack_session_notification
        AFTER INSERT OR UPDATE OF status,ended_at ON work_sessions
        FOR EACH ROW EXECUTE FUNCTION dayfinch_enqueue_slack_session_notification();

        CREATE OR REPLACE FUNCTION dayfinch_enqueue_slack_todo_notification()
        RETURNS TRIGGER AS $$
        BEGIN
            IF OLD.completed_at IS NOT NULL OR NEW.completed_at IS NULL THEN
                RETURN NEW;
            END IF;
            INSERT INTO slack_outbox(
                id,integration_id,destination_id,event_key,event_type,user_id,
                project_id,todo_id,occurred_at,next_attempt_at,created_at,updated_at
            )
            SELECT gen_random_uuid(),i.id,d.id,
                   'todo:' || NEW.todo_id::text || ':project:' ||
                       NEW.project_id::text || ':completed:' ||
                       NEW.completed_at::text,
                   'todo_completed',NEW.completed_by_user_id,NEW.project_id,
                   NEW.todo_id,NEW.completed_at,CURRENT_TIMESTAMP,
                   CURRENT_TIMESTAMP,CURRENT_TIMESTAMP
            FROM integrations i
            JOIN slack_notification_defaults defaults
              ON defaults.integration_id=i.id
            JOIN slack_destinations d
              ON d.integration_id=i.id AND d.enabled=TRUE
            LEFT JOIN slack_notification_users member_rule
              ON member_rule.integration_id=i.id
             AND member_rule.user_id=NEW.completed_by_user_id
            WHERE i.provider='slack' AND i.enabled=TRUE
              AND COALESCE(member_rule.todo_events,defaults.todo_events)=TRUE
            ON CONFLICT(destination_id,event_key) DO NOTHING;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        DROP TRIGGER IF EXISTS trg_slack_todo_notification ON project_todos;
        CREATE TRIGGER trg_slack_todo_notification
        AFTER UPDATE OF completed_at ON project_todos
        FOR EACH ROW EXECUTE FUNCTION dayfinch_enqueue_slack_todo_notification();
        """
    )


def _add_slack_dead_letter_state(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE slack_outbox
            ADD COLUMN IF NOT EXISTS discarded_at TIMESTAMPTZ;
        DROP INDEX IF EXISTS idx_slack_outbox_due;
        CREATE INDEX idx_slack_outbox_due
            ON slack_outbox(next_attempt_at,claim_until,created_at)
            WHERE sent_at IS NULL AND discarded_at IS NULL;
        CREATE INDEX IF NOT EXISTS idx_slack_outbox_retention
            ON slack_outbox(COALESCE(sent_at,discarded_at))
            WHERE sent_at IS NOT NULL OR discarded_at IS NOT NULL;
        """
    )


def _index_audit_report_filters(connection: Connection) -> None:
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_audit_actor_occurred
            ON audit_events(actor_user_id,occurred_at DESC,id);
        CREATE INDEX IF NOT EXISTS idx_audit_action_occurred
            ON audit_events(action,occurred_at DESC,id);
        CREATE INDEX IF NOT EXISTS idx_audit_target_occurred
            ON audit_events(target_type,occurred_at DESC,id);
        """
    )


def _create_member_tracking_settings(connection: Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS user_tracking_settings (
            user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            screenshot_frequency INTEGER CHECK(
                screenshot_frequency BETWEEN 0 AND 3
            ),
            screenshot_blur BOOLEAN,
            track_apps BOOLEAN,
            track_urls BOOLEAN,
            idle_timeout_minutes INTEGER CHECK(
                idle_timeout_minutes BETWEEN 1 AND 1440
            ),
            allow_screenshot_delete BOOLEAN,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_users_enabled_email_prefix
            ON users(lower(email) text_pattern_ops) WHERE enabled=TRUE;
        CREATE INDEX IF NOT EXISTS idx_users_enabled_name_prefix
            ON users(lower(full_name) text_pattern_ops) WHERE enabled=TRUE;
        """
    )


def _add_allowed_tracking_apps(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE organization_settings
            ADD COLUMN IF NOT EXISTS allowed_apps TEXT NOT NULL DEFAULT 'all';
        ALTER TABLE organization_settings
            DROP CONSTRAINT IF EXISTS organization_allowed_apps_check;
        ALTER TABLE organization_settings
            ADD CONSTRAINT organization_allowed_apps_check
                CHECK(allowed_apps IN ('all','desktop_only'));
        ALTER TABLE user_tracking_settings
            ADD COLUMN IF NOT EXISTS allowed_apps TEXT;
        ALTER TABLE user_tracking_settings
            DROP CONSTRAINT IF EXISTS user_tracking_allowed_apps_check;
        ALTER TABLE user_tracking_settings
            ADD CONSTRAINT user_tracking_allowed_apps_check
                CHECK(allowed_apps IS NULL OR allowed_apps IN ('all','desktop_only'));
        """
    )


def _classify_tracker_devices(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE devices
            ADD COLUMN IF NOT EXISTS tracker_kind TEXT NOT NULL DEFAULT 'desktop';
        UPDATE devices
        SET tracker_kind=CASE
            WHEN platform='Web timer' THEN 'web'
            WHEN platform ILIKE 'Dayfinch Mobile%%' THEN 'mobile'
            ELSE 'desktop'
        END;
        ALTER TABLE devices
            DROP CONSTRAINT IF EXISTS devices_tracker_kind_check;
        ALTER TABLE devices
            ADD CONSTRAINT devices_tracker_kind_check
                CHECK(tracker_kind IN ('desktop','mobile','web'));
        CREATE INDEX IF NOT EXISTS idx_devices_owner_tracker_kind
            ON devices(owner_user_id,tracker_kind) WHERE enabled=TRUE;
        """
    )


def _create_direct_payroll_destinations(connection: Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS payroll_destinations (
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider TEXT NOT NULL CHECK(provider IN ('paypal')),
            recipient TEXT NOT NULL,
            confirmed_at TIMESTAMPTZ NOT NULL,
            updated_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id,provider)
        );
        ALTER TABLE payroll_payments
            ADD COLUMN IF NOT EXISTS recipient TEXT NOT NULL DEFAULT '';
        CREATE INDEX IF NOT EXISTS idx_payroll_reconciliation
            ON payroll_payments(provider,status,updated_at)
            WHERE status='processing';
        """
    )


def _harden_payroll_calculation(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE organization_settings
            ADD COLUMN IF NOT EXISTS overtime_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            ADD COLUMN IF NOT EXISTS weekly_overtime_minutes INTEGER NOT NULL DEFAULT 2400,
            ADD COLUMN IF NOT EXISTS overtime_multiplier NUMERIC(5,2) NOT NULL DEFAULT 1.50;
        ALTER TABLE organization_settings
            DROP CONSTRAINT IF EXISTS organization_weekly_overtime_minutes_check;
        ALTER TABLE organization_settings
            ADD CONSTRAINT organization_weekly_overtime_minutes_check
                CHECK(weekly_overtime_minutes BETWEEN 0 AND 10080);
        ALTER TABLE organization_settings
            DROP CONSTRAINT IF EXISTS organization_overtime_multiplier_check;
        ALTER TABLE organization_settings
            ADD CONSTRAINT organization_overtime_multiplier_check
                CHECK(overtime_multiplier BETWEEN 1 AND 10);
        ALTER TABLE payroll_payments
            ADD COLUMN IF NOT EXISTS pay_rate_snapshot NUMERIC(12,2) NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS overtime_multiplier_snapshot NUMERIC(5,2)
                NOT NULL DEFAULT 1.50,
            ADD COLUMN IF NOT EXISTS source_timesheet_id UUID
                REFERENCES timesheets(id) ON DELETE RESTRICT;
        CREATE INDEX IF NOT EXISTS idx_payroll_member_period
            ON payroll_payments(user_id,period_start,period_end);
        """
    )


def _bound_paypal_idempotency_window(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE payroll_payments
            ADD COLUMN IF NOT EXISTS delivery_started_at TIMESTAMPTZ;
        UPDATE payroll_payments SET delivery_started_at=updated_at
            WHERE provider <> 'manual' AND delivery_started_at IS NULL;
        """
    )


def _create_wise_payroll_destinations(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE payroll_destinations
            ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT '';
        ALTER TABLE payroll_destinations
            DROP CONSTRAINT IF EXISTS payroll_destinations_provider_check;
        ALTER TABLE payroll_destinations
            ADD CONSTRAINT payroll_destinations_provider_check
                CHECK(provider IN ('paypal','wise'));
        ALTER TABLE payroll_payments
            ADD COLUMN IF NOT EXISTS recipient_currency TEXT NOT NULL DEFAULT '';
        """
    )


def _track_post_payment_reversals(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE payroll_payments
            ADD COLUMN IF NOT EXISTS reversed_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS next_reconcile_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS reconcile_until TIMESTAMPTZ;
        ALTER TABLE payroll_payments
            DROP CONSTRAINT IF EXISTS payroll_payments_status_check;
        ALTER TABLE payroll_payments
            ADD CONSTRAINT payroll_payments_status_check
                CHECK(status IN ('draft','processing','paid','failed','reversed'));
        UPDATE payroll_payments
        SET next_reconcile_at=COALESCE(next_reconcile_at,updated_at)
        WHERE status='processing' AND external_reference <> '';
        UPDATE payroll_payments
        SET next_reconcile_at=COALESCE(next_reconcile_at,CURRENT_TIMESTAMP),
            reconcile_until=COALESCE(
                reconcile_until,
                paid_at + INTERVAL '90 days'
            )
        WHERE status='paid' AND provider IN ('paypal','wise')
          AND external_reference <> '' AND paid_at IS NOT NULL
          AND paid_at + INTERVAL '90 days' > CURRENT_TIMESTAMP;
        DROP INDEX IF EXISTS idx_payroll_reconciliation;
        CREATE INDEX idx_payroll_reconciliation
            ON payroll_payments(provider,next_reconcile_at,id)
            WHERE status IN ('processing','paid') AND external_reference <> '';
        """
    )


def _add_payroll_reconciliation_backoff(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE payroll_payments
            ADD COLUMN IF NOT EXISTS reconcile_attempts INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS last_reconcile_error TEXT NOT NULL DEFAULT '';
        ALTER TABLE payroll_payments
            DROP CONSTRAINT IF EXISTS payroll_reconcile_attempts_check;
        ALTER TABLE payroll_payments
            ADD CONSTRAINT payroll_reconcile_attempts_check
                CHECK(reconcile_attempts BETWEEN 0 AND 20);
        UPDATE payroll_payments
        SET next_reconcile_at=updated_at
        WHERE status='processing' AND external_reference <> ''
          AND next_reconcile_at IS NULL;
        DROP INDEX IF EXISTS idx_payroll_reconciliation;
        CREATE INDEX idx_payroll_reconciliation
            ON payroll_payments(provider,next_reconcile_at,id)
            WHERE status IN ('processing','paid') AND external_reference <> '';
        """
    )


def _create_wise_payroll_webhook_queue(connection: Connection) -> None:
    connection.execute(
        """
        ALTER TABLE payroll_payments
            ADD COLUMN IF NOT EXISTS provider_event_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS provider_status TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS provider_failure_code TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS provider_failure_description TEXT NOT NULL DEFAULT '';
        CREATE UNIQUE INDEX IF NOT EXISTS uq_payroll_direct_provider_reference
            ON payroll_payments(provider,external_reference)
            WHERE provider IN ('paypal','wise') AND external_reference <> '';
        DROP INDEX IF EXISTS idx_payroll_reconciliation;
        CREATE INDEX idx_payroll_reconciliation
            ON payroll_payments(provider,next_reconcile_at,id)
            WHERE status IN ('processing','paid','failed')
              AND external_reference <> '';
        """
    )


def _create_request_rate_limits(connection: Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS request_rate_limits (
               bucket_key TEXT PRIMARY KEY,
               window_id BIGINT NOT NULL,
               request_count INTEGER NOT NULL CHECK(request_count > 0),
               updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
           );
           CREATE INDEX IF NOT EXISTS idx_request_rate_limits_updated
           ON request_rate_limits(updated_at);"""
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
    Migration(14, "create_login_throttle", _create_login_throttle),
    Migration(15, "index_login_throttle_cleanup", _index_login_throttle_cleanup),
    Migration(16, "create_background_job_leases", _create_background_job_leases),
    Migration(17, "create_oidc_identities", _create_oidc_identities),
    Migration(18, "create_scim_identities", _create_scim_identities),
    Migration(19, "create_project_roles", _create_project_roles),
    Migration(
        20,
        "replace_global_viewers_with_project_viewers",
        _replace_global_viewers_with_project_viewers,
    ),
    Migration(21, "create_team_lead_permissions", _create_team_lead_permissions),
    Migration(22, "create_team_projects", _create_team_projects),
    Migration(23, "create_scim_groups", _create_scim_groups),
    Migration(24, "index_active_work_lifecycle", _index_active_work_lifecycle),
    Migration(25, "create_saved_report_filters", _create_saved_report_filters),
    Migration(26, "create_team_invoices", _create_team_invoices),
    Migration(
        27,
        "create_automatic_tracking_policies",
        _create_automatic_tracking_policies,
    ),
    Migration(28, "enforce_single_active_timer", _enforce_single_active_timer),
    Migration(29, "add_scheduled_report_formats", _add_scheduled_report_formats),
    Migration(30, "add_scheduled_report_calendar", _add_scheduled_report_calendar),
    Migration(
        31,
        "harden_scheduled_report_delivery",
        _harden_scheduled_report_delivery,
    ),
    Migration(
        32,
        "claim_scheduled_report_delivery",
        _claim_scheduled_report_delivery,
    ),
    Migration(
        33,
        "create_quickbooks_export_mappings",
        _create_quickbooks_export_mappings,
    ),
    Migration(34, "add_quickbooks_timezone", _add_quickbooks_timezone),
    Migration(35, "create_github_app_integration", _create_github_app_integration),
    Migration(
        36,
        "add_github_incremental_sync_cursors",
        _add_github_incremental_sync_cursors,
    ),
    Migration(
        37,
        "protect_github_full_reconciliation",
        _protect_github_full_reconciliation,
    ),
    Migration(
        38,
        "create_integration_credential_vault",
        _create_integration_credential_vault,
    ),
    Migration(
        39,
        "create_jira_cloud_integration",
        _create_jira_cloud_integration,
    ),
    Migration(
        40,
        "protect_jira_eventual_reconciliation",
        _protect_jira_eventual_reconciliation,
    ),
    Migration(
        41,
        "retain_tasks_after_integration_disconnect",
        _retain_tasks_after_integration_disconnect,
    ),
    Migration(
        42,
        "create_integration_user_credentials",
        _create_integration_user_credentials,
    ),
    Migration(43, "create_jira_worklog_delivery", _create_jira_worklog_delivery),
    Migration(
        44,
        "create_jira_authorization_periods",
        _create_jira_authorization_periods,
    ),
    Migration(45, "create_asana_cloud_integration", _create_asana_cloud_integration),
    Migration(
        46,
        "create_asana_time_comment_delivery",
        _create_asana_time_comment_delivery,
    ),
    Migration(47, "create_slack_notifications", _create_slack_notifications),
    Migration(48, "add_slack_dead_letter_state", _add_slack_dead_letter_state),
    Migration(49, "index_audit_report_filters", _index_audit_report_filters),
    Migration(50, "create_member_tracking_settings", _create_member_tracking_settings),
    Migration(51, "add_allowed_tracking_apps", _add_allowed_tracking_apps),
    Migration(52, "create_saml_replay_guard", _create_saml_replay_guard),
    Migration(53, "classify_tracker_devices", _classify_tracker_devices),
    Migration(
        54,
        "create_direct_payroll_destinations",
        _create_direct_payroll_destinations,
    ),
    Migration(55, "harden_payroll_calculation", _harden_payroll_calculation),
    Migration(
        56,
        "bound_paypal_idempotency_window",
        _bound_paypal_idempotency_window,
    ),
    Migration(
        57,
        "create_wise_payroll_destinations",
        _create_wise_payroll_destinations,
    ),
    Migration(
        58,
        "track_post_payment_reversals",
        _track_post_payment_reversals,
    ),
    Migration(
        59,
        "add_payroll_reconciliation_backoff",
        _add_payroll_reconciliation_backoff,
    ),
    Migration(
        60,
        "create_wise_payroll_webhook_queue",
        _create_wise_payroll_webhook_queue,
    ),
    Migration(61, "create_request_rate_limits", _create_request_rate_limits),
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

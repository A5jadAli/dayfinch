import os
from urllib.parse import urlparse

import pytest

from api.database import Database


@pytest.fixture
def postgres_url() -> str:
    value = os.getenv("TRACKER_TEST_DATABASE_URL")
    if not value:
        pytest.skip("TRACKER_TEST_DATABASE_URL is required for PostgreSQL tests")
    database_name = urlparse(value).path.removeprefix("/")
    if not database_name.endswith("_test"):
        pytest.fail(
            "Refusing to truncate a non-test database; "
            "TRACKER_TEST_DATABASE_URL must end in _test"
        )
    cleaner = Database(value, min_pool_size=1, max_pool_size=2)
    cleaner.initialize()
    with cleaner.connect() as connection:
        connection.execute(
            """TRUNCATE TABLE
                   request_rate_limits, saml_assertion_replays, background_job_leases, login_attempts, scheduled_reports,
                   saved_report_filters,
                   jira_worklog_exports, jira_worklog_dirty_days,
                   jira_user_authorization_periods, jira_user_connections,
                   asana_comment_exports, asana_comment_dirty_days,
                   asana_user_authorization_periods,
                   asana_task_assignees, asana_user_connections,
                   asana_project_mappings, integration_oauth_pending,
                   slack_outbox, slack_destinations, slack_notification_users,
                   slack_notification_defaults,
                   integration_user_credentials, integration_credentials,
                   integration_webhook_deliveries,
                   jira_project_mappings, integration_project_mappings,
                   automatic_tracking_assignments, automatic_tracking_policies,
                   user_tracking_settings,
                   project_todos, global_todos, integrations,
                   usage_records,
                   location_events, geofences, holidays, work_breaks,
                   notifications, team_invoice_payments, team_invoice_time_sources,
                   team_invoice_lines, team_invoices,
                   payroll_destinations, payroll_payments, invoice_lines, invoices, expenses,
                   time_off_requests, shifts, manual_time_entries, team_projects,
                   team_leads, team_members, teams,
                   clients, organization_settings, audit_events, activity_records,
                   timesheets, work_session_segments,
                   work_sessions, tasks, invitations, devices, project_members,
                   projects, users
               RESTART IDENTITY CASCADE"""
        )
        connection.execute("INSERT INTO organization_settings(id) VALUES (1)")
    cleaner.close()
    return value


@pytest.fixture
def database(postgres_url: str):
    database = Database(postgres_url, min_pool_size=1, max_pool_size=2)
    database.initialize()
    try:
        yield database
    finally:
        database.close()

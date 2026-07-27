import os

import pytest

from tracker_server.database import Database


@pytest.fixture
def postgres_url() -> str:
    value = os.getenv("TRACKER_TEST_DATABASE_URL")
    if not value:
        pytest.skip("TRACKER_TEST_DATABASE_URL is required for PostgreSQL tests")
    cleaner = Database(value, min_pool_size=1, max_pool_size=2)
    cleaner.initialize()
    with cleaner.connect() as connection:
        connection.execute(
            """TRUNCATE TABLE
                   audit_events, activity_records, timesheets, work_session_segments,
                   work_sessions, tasks, invitations, devices, project_members,
                   projects, users
               RESTART IDENTITY CASCADE"""
        )
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

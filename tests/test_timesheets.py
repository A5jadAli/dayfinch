from datetime import date

import pytest

from tracker_server.database import Database
from tracker_server.services.timesheets import TimesheetService


def _tracked_user(database: Database):
    admin = database.bootstrap_admin("admin@example.com", "hash")
    user_id = "11111111-1111-1111-1111-111111111111"
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO users(id, email, password_hash, role, enabled, created_at)
               VALUES (%s, 'dev@example.com', 'hash', 'member', TRUE,
                       '2026-07-01T00:00:00+00:00')""",
            (user_id,),
        )
    user = database.get_user(user_id)
    project = database.create_project("Alpha", "", admin["id"])
    _, token = database.create_device("Laptop", user_id, project["id"])
    return admin, user, database.authenticate_device(token)


def test_submit_review_and_lock_timesheet(database: Database):
    admin, user, device = _tracked_user(database)
    session = database.sync_work_session(device, "active", None)
    database.sync_work_session(device, "stopped", None)
    with database.connect() as connection:
        connection.execute(
            """UPDATE work_session_segments
               SET started_at = '2026-07-20T09:00:00+00:00',
                   ended_at = '2026-07-20T10:30:00+00:00'
               WHERE session_id = %s""",
            (session["id"],),
        )
        connection.execute(
            "UPDATE work_sessions SET started_at = '2026-07-20T09:00:00+00:00' WHERE id = %s",
            (session["id"],),
        )

    service = TimesheetService(database)
    sheet = service.submit(user, "2026-07-20", "2026-07-26")
    rows = database.list_timesheets(user["id"])
    assert rows[0]["tracked_seconds"] == 5_400

    approved = service.review(admin, sheet["id"], "approved", "Verified")
    assert approved["status"] == "approved"
    assert database.is_period_locked(user["id"], date(2026, 7, 20))
    with pytest.raises(ValueError, match="locked"):
        service.submit(user, "2026-07-20", "2026-07-26")


def test_active_session_must_stop_before_submission(database: Database):
    _, user, device = _tracked_user(database)
    database.sync_work_session(device, "active", None)

    with pytest.raises(ValueError, match="Stop the active"):
        TimesheetService(database).submit(user, "2026-07-20", "2026-07-26")


def test_timesheet_period_validation():
    with pytest.raises(ValueError, match="before"):
        TimesheetService.parse_period("2026-07-27", "2026-07-20")
    with pytest.raises(ValueError, match="32 days"):
        TimesheetService.parse_period("2026-01-01", "2026-03-01")

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from api.config import Settings
from api.main import create_app
from api.security import hash_password
from api.services.quickbooks_iif import (
    QuickBooksIIFError,
    quickbooks_timer_iif,
    validate_mapping,
)


def _row(**overrides):
    row = {
        "work_date": date(2008, 1, 5),
        "quickbooks_name": "Joe Employee",
        "quickbooks_customer_job": "Customer:Job",
        "quickbooks_service_item": "Engineering",
        "quickbooks_class": "Delivery",
        "quickbooks_billable": True,
        "note": "Notes about the activity",
        "seconds": 4 * 3600 + 15 * 60,
    }
    row.update(overrides)
    return row


def _export(rows):
    return quickbooks_timer_iif(
        company_name="Import Test Company",
        company_create_time="1208544781",
        default_service_item="General labor",
        rows=rows,
        generated_at=datetime(2008, 4, 18, 12, tzinfo=UTC),
    )


def test_iif_matches_intuit_timer_header_and_time_activity_layout():
    export = _export([_row()])
    lines = export.data.decode("ascii").splitlines()

    assert lines[0].split("\t") == [
        "!TIMERHDR",
        "VER",
        "REL",
        "COMPANYNAME",
        "IMPORTEDBEFORE",
        "FROMTIMER",
        "COMPANYCREATETIME",
    ]
    assert lines[1].split("\t") == [
        "TIMERHDR",
        "8",
        "0",
        "Import Test Company",
        "N",
        "Y",
        "1208544781",
    ]
    assert lines[4].split("\t") == [
        "!TIMEACT",
        "DATE",
        "JOB",
        "EMP",
        "ITEM",
        "PITEM",
        "DURATION",
        "PROJ",
        "NOTE",
        "BILLINGSTATUS",
    ]
    assert lines[5].split("\t") == [
        "TIMEACT",
        "01/05/08",
        "Customer:Job",
        "Joe Employee",
        "Engineering",
        "",
        "4:15",
        "Delivery",
        "Notes about the activity",
        "1",
    ]
    assert export.row_count == 1
    assert export.source_seconds == 15_300
    assert export.exported_minutes == 255


def test_iif_rounding_splitting_and_note_sanitization_are_deterministic():
    export = _export(
        [
            _row(seconds=29, note="discarded"),
            _row(seconds=30, note="=formula\r\nCafé\tvalue"),
            _row(seconds=24 * 3600, quickbooks_billable=False),
        ]
    )
    activities = [
        line.split("\t")
        for line in export.data.decode("ascii").splitlines()
        if line.startswith("TIMEACT\t")
    ]

    assert [activity[6] for activity in activities] == ["0:01", "23:59", "0:01"]
    assert activities[0][8] == "'=formula/nCafe value"
    assert activities[1][9] == "0"
    assert export.skipped_seconds == 29
    assert export.exported_minutes == 1_441


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("Bad\tName", "control characters"),
        ("München", "ASCII"),
        ("=CMD()", "formula"),
    ],
)
def test_exact_quickbooks_mappings_reject_unsafe_values(value, message):
    with pytest.raises(QuickBooksIIFError, match=message):
        validate_mapping(value, "QuickBooks name")


def test_iif_rejects_missing_company_identity_and_billable_job_mapping():
    with pytest.raises(QuickBooksIIFError, match="creation time"):
        quickbooks_timer_iif(
            company_name="Company",
            company_create_time="",
            default_service_item="Labor",
            rows=[],
        )
    with pytest.raises(QuickBooksIIFError, match="customer/job"):
        _export([_row(quickbooks_customer_job="")])


def _settings(tmp_path, postgres_url: str) -> Settings:
    return Settings(
        data_dir=tmp_path,
        admin_email="owner@example.test",
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024 * 1024,
        retention_days=30,
        database_url=postgres_url,
    )


def _csrf(response) -> str:
    matched = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert matched
    return matched.group(1)


def _login(client: TestClient, email: str, password: str) -> None:
    client.cookies.clear()
    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "email": email,
            "password": password,
            "csrf": _csrf(page),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_quickbooks_mapping_approval_and_export_workflow(tmp_path, postgres_url):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        owner = database.get_user_by_email("owner@example.test")
        _, invite_token = database.create_invitation(
            "developer@example.test", owner["id"], 24
        )
        member = database.accept_invitation(
            invite_token, hash_password("developer password long")
        )
        database.set_user_profile(member["id"], "member", "Developer One", 0, 0, 0)
        project = database.create_project("Client project", "", owner["id"])
        database.add_project_member(project["id"], member["id"], "worker")
        task = database.create_task(
            project["id"], "Build feature", "", owner["id"], billable=True
        )
        _, device_token = database.create_device(
            "Developer laptop", member["id"], project["id"]
        )
        device = database.authenticate_device(device_token)
        ended_at = datetime.now(UTC) - timedelta(minutes=1)
        started_at = ended_at - timedelta(minutes=15)
        database.sync_work_session(
            device,
            "active",
            task["id"],
            project["id"],
            "Implemented export",
            observed_at=started_at,
            transition=True,
        )
        midnight = ended_at.replace(hour=0, minute=0, second=0, microsecond=0)
        manual_started_at = midnight - timedelta(minutes=5)
        database.add_manual_time(
            member["id"],
            project["id"],
            task["id"],
            manual_started_at,
            midnight + timedelta(minutes=5),
            "Across midnight",
            auto_approve=True,
        )
        database.sync_work_session(
            device,
            "stopped",
            None,
            project["id"],
            observed_at=ended_at,
            transition=True,
        )

        _login(client, owner["email"], "correct horse battery staple")
        page = client.get("/reports/quickbooks")
        assert page.status_code == 200
        csrf = _csrf(page)
        params = {
            "date_from": manual_started_at.date().isoformat(),
            "date_to": ended_at.date().isoformat(),
        }
        assert (
            client.post(
                "/reports/quickbooks/settings",
                data={
                    "csrf": csrf,
                    "company_name": "Acme Company",
                    "company_create_time": "1208544781",
                    "default_service_item": "General labor",
                    "timezone_name": "UTC",
                },
                follow_redirects=False,
            ).status_code
            == 303
        )
        unmapped = client.get(
            "/reports/quickbooks.iif",
            params={**params, "approval_scope": "finalized"},
        )
        assert unmapped.status_code == 422
        assert "employee name" in unmapped.text
        assert (
            client.post(
                f"/reports/quickbooks/users/{member['id']}",
                data={"csrf": csrf, "quickbooks_name": "Developer One"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert (
            client.post(
                f"/reports/quickbooks/projects/{project['id']}",
                data={
                    "csrf": csrf,
                    "customer_job": "Acme:Platform",
                    "class_name": "Engineering",
                    "billable": "true",
                },
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert (
            client.post(
                f"/reports/quickbooks/tasks/{task['id']}",
                data={"csrf": csrf, "service_item": "Software development"},
                follow_redirects=False,
            ).status_code
            == 303
        )

        approved_empty = client.get("/reports/quickbooks.iif", params=params)
        assert approved_empty.status_code == 200
        assert b"\r\nTIMEACT\t" not in approved_empty.content

        sheet = database.submit_timesheet(
            member["id"], manual_started_at.date(), ended_at.date()
        )
        database.review_timesheet(sheet["id"], owner["id"], "approved", "")
        mapped_rows = database.quickbooks_time_rows(
            datetime.combine(manual_started_at.date(), datetime.min.time(), UTC),
            datetime.combine(
                ended_at.date() + timedelta(days=1), datetime.min.time(), UTC
            ),
        )
        midnight_rows = [row for row in mapped_rows if row["note"] == "Across midnight"]
        assert [row["seconds"] for row in midnight_rows] == [300, 300]
        exported = client.get("/reports/quickbooks.iif", params=params)
        assert exported.status_code == 200
        assert exported.headers["content-type"] == "application/octet-stream"
        assert exported.headers["cache-control"] == "private, no-store"
        text = exported.content.decode("ascii")
        tracked_rows = [
            line.split("\t")
            for line in text.splitlines()
            if line.startswith("TIMEACT\t") and "Implemented export" in line
        ]
        assert (
            sum(
                int(row[6].split(":", 1)[0]) * 60 + int(row[6].split(":", 1)[1])
                for row in tracked_rows
            )
            == 15
        )
        assert text.rstrip().endswith("\t1")
        with database.connect() as connection:
            audit = connection.execute(
                """SELECT details FROM audit_events
                   WHERE action='quickbooks.time_exported'
                   ORDER BY occurred_at DESC LIMIT 1"""
            ).fetchone()
        assert "source_seconds=" in audit["details"]
        assert "exported_minutes=" in audit["details"]
        assert "Developer One" not in audit["details"]

        dst_start = datetime(2026, 3, 8, 4, 30, tzinfo=UTC)
        dst_end = datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
        database.add_manual_time(
            member["id"],
            project["id"],
            task["id"],
            dst_start,
            dst_end,
            "DST boundary",
            auto_approve=True,
        )
        dst_rows = database.quickbooks_time_rows(
            datetime(2026, 3, 7, 5, tzinfo=UTC),
            datetime(2026, 3, 9, 4, tzinfo=UTC),
            approved_only=False,
            timezone_name="America/New_York",
        )
        dst_rows = [row for row in dst_rows if row["note"] == "DST boundary"]
        assert [str(row["work_date"]) for row in dst_rows] == [
            "2026-03-07",
            "2026-03-08",
        ]
        assert [row["seconds"] for row in dst_rows] == [1_800, 9_000]

        with pytest.raises(QuickBooksIIFError, match="IANA"):
            database.update_quickbooks_export_settings(
                "Acme Company", "1208544781", "General labor", "Invalid/Nowhere"
            )

        _login(client, member["email"], "developer password long")
        assert client.get("/reports/quickbooks").status_code == 403

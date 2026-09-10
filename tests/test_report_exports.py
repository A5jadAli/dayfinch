from __future__ import annotations

import calendar
import csv
import io
import re
import uuid
import zipfile
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.config import Settings
from api.main import create_app
from api.routers.reports import _csv_bytes, _group_columns, _group_custom_report_rows
from api.security import hash_password
from api.services.pdf_export import PDFExportBusy
from api.services.report_delivery import ReportDeliveryService
from api.services.zipstream import ZipEntry, stream_zip


def _settings(tmp_path, postgres_url: str) -> Settings:
    return Settings(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024 * 1024,
        retention_days=30,
        admin_email="owner@example.test",
        database_url=postgres_url,
    )


def _member(database, owner: dict, email: str) -> dict:
    _, token = database.create_invitation(email, owner["id"], 24)
    return database.accept_invitation(
        token, hash_password("member password long enough")
    )


def _csrf(response) -> str:
    matched = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert matched, response.text
    return matched.group(1)


def _login(
    client: TestClient,
    email: str,
    password: str = "member password long enough",
) -> None:
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


def _capture(
    client: TestClient, database, member: dict, project: dict, app: str
) -> str:
    device, token = database.create_device(
        f"{member['email']} laptop", member["id"], project["id"]
    )
    captured = datetime.now(UTC) - timedelta(seconds=10)
    session = database.sync_work_session(
        device,
        "active",
        None,
        project["id"],
        observed_at=captured - timedelta(seconds=10),
    )
    record_id = str(uuid.uuid4())
    response = client.post(
        "/api/v1/activity",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "record_id": record_id,
            "captured_at": captured.isoformat(),
            "keyboard_events": "5",
            "mouse_clicks": "3",
            "mouse_distance": "120",
            "active_app": app,
            "agent_version": "0.6.0",
            "focused_seconds": "60",
            "interactive_seconds": "30",
            "session_id": session["id"],
        },
        files={
            "screenshot_file": (
                "capture.jpg",
                b"\xff\xd8\xffexport-test",
                "image/jpeg",
            )
        },
    )
    assert response.status_code == 201, response.text
    return record_id


def test_zip_stream_is_readable_and_rejects_unsafe_names():
    now = datetime.now(UTC)
    archive = b"".join(
        stream_zip(
            [
                ZipEntry("folder/first.txt", b"first", now),
                ZipEntry("second.txt", b"second", now),
            ]
        )
    )
    with zipfile.ZipFile(io.BytesIO(archive)) as result:
        assert result.namelist() == ["folder/first.txt", "second.txt"]
        assert result.read("folder/first.txt") == b"first"

    try:
        b"".join(stream_zip([ZipEntry("../private", b"bad", now)]))
    except ValueError as exc:
        assert "unsafe" in str(exc)
    else:
        raise AssertionError("unsafe ZIP path was accepted")


def test_csv_export_neutralizes_spreadsheet_formulas():
    result = _csv_bytes(
        ["name", "description"],
        [{"name": "=2+2", "description": "  @SUM(A1:A2)"}],
    ).decode()

    assert "'=2+2" in result
    assert "'  @SUM(A1:A2)" in result
    assert "'=2+2" in ReportDeliveryService._csv([{"value": "=2+2"}]).decode()


def test_scheduled_pdf_delivery_uses_a_real_pdf_attachment(monkeypatch):
    sent_messages = []
    marked = []
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)

    class Database:
        @staticmethod
        def due_scheduled_reports(_sent_at, claim_token):
            uuid.UUID(claim_token)
            return [
                {
                    "id": "report-id",
                    "name": "Weekly time",
                    "report_type": "time",
                    "frequency": "weekly",
                    "delivery_format": "pdf",
                    "delivery_hour": 9,
                    "delivery_minute": 0,
                    "schedule_weekday": 0,
                    "schedule_month_day": 1,
                    "recipients": "finance@example.test",
                }
            ]

        @staticmethod
        def time_report(**kwargs):
            assert kwargs == {
                "started_at": datetime(2026, 8, 31, tzinfo=UTC),
                "ended_at": datetime(2026, 9, 7, tzinfo=UTC),
                "limit": 1_001,
            }
            return [{"member": "Developer", "seconds": 3600}]

        @staticmethod
        def mark_scheduled_report_sent(report_id, sent_at, next_send_at, claim_token):
            uuid.UUID(claim_token)
            marked.append((report_id, sent_at, next_send_at))

        @staticmethod
        def mark_scheduled_report_failed(*_args):
            raise AssertionError("successful delivery must not be marked failed")

    class SMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def send_message(message):
            sent_messages.append(message)

    settings = SimpleNamespace(
        smtp_host="smtp.example.test",
        smtp_from_email="dayfinch@example.test",
        smtp_port=25,
        smtp_timeout_seconds=10,
        smtp_starttls=False,
        smtp_username="",
        smtp_password="",
    )
    monkeypatch.setattr("api.services.report_delivery.smtplib.SMTP", SMTP)

    delivered = ReportDeliveryService(Database(), settings).deliver_due(now)

    assert delivered == 1
    assert marked == [("report-id", now, datetime(2026, 9, 14, 9, tzinfo=UTC))]
    attachment = next(sent_messages[0].iter_attachments())
    assert attachment.get_content_type() == "application/pdf"
    assert attachment.get_filename() == "dayfinch-time.pdf"
    assert attachment.get_payload(decode=True).startswith(b"%PDF-")
    with pytest.raises(ValueError, match="exceeds 1000 rows"):
        ReportDeliveryService._pdf("Oversized", "time", [{"seconds": 1}] * 1_001)


def test_scheduled_delivery_failure_is_backed_off_and_does_not_block_next_report(
    monkeypatch,
):
    now = datetime(2026, 9, 9, 12, tzinfo=UTC)
    sent_messages = []
    marked_sent = []
    marked_failed = []
    reports = [
        {
            "id": "bad-report",
            "name": "Too large",
            "report_type": "time",
            "frequency": "daily",
            "delivery_format": "pdf",
            "delivery_hour": 9,
            "delivery_minute": 0,
            "schedule_weekday": 0,
            "schedule_month_day": 1,
            "range_preset": "previous_day",
            "consecutive_failures": 2,
            "recipients": "finance@example.test",
        },
        {
            "id": "good-report",
            "name": "Daily activity",
            "report_type": "activity",
            "frequency": "daily",
            "delivery_format": "csv",
            "delivery_hour": 9,
            "delivery_minute": 0,
            "schedule_weekday": 0,
            "schedule_month_day": 1,
            "range_preset": "previous_day",
            "consecutive_failures": 0,
            "recipients": "finance@example.test",
        },
    ]

    class Database:
        @staticmethod
        def due_scheduled_reports(_sent_at, claim_token):
            uuid.UUID(claim_token)
            return reports

        @staticmethod
        def time_report(**_kwargs):
            return [{"seconds": 1}] * 1_001

        @staticmethod
        def activity_report(**kwargs):
            assert kwargs["started_at"] == datetime(2026, 9, 8, tzinfo=UTC)
            assert kwargs["ended_at"] == datetime(2026, 9, 9, tzinfo=UTC)
            assert kwargs["limit"] == 10_001
            return [{"member": "Developer", "interactive_seconds": 120}]

        @staticmethod
        def mark_scheduled_report_sent(report_id, sent_at, next_send_at, claim_token):
            uuid.UUID(claim_token)
            marked_sent.append((report_id, sent_at, next_send_at))

        @staticmethod
        def mark_scheduled_report_failed(
            report_id, failed_at, next_attempt_at, error_code, claim_token
        ):
            uuid.UUID(claim_token)
            marked_failed.append((report_id, failed_at, next_attempt_at, error_code))

    class SMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def send_message(message):
            sent_messages.append(message)

    settings = SimpleNamespace(
        smtp_host="smtp.example.test",
        smtp_from_email="dayfinch@example.test",
        smtp_port=25,
        smtp_timeout_seconds=10,
        smtp_starttls=False,
        smtp_username="",
        smtp_password="",
    )
    monkeypatch.setattr("api.services.report_delivery.smtplib.SMTP", SMTP)

    delivered = ReportDeliveryService(Database(), settings).deliver_due(now)

    assert delivered == 1
    assert len(sent_messages) == 1
    assert marked_sent[0][0] == "good-report"
    assert marked_failed[0][0:2] == ("bad-report", now)
    assert marked_failed[0][2] > now + timedelta(minutes=19)
    assert marked_failed[0][3] == "report_too_large"


def test_collapsed_report_totals_weight_activity_and_keep_group_identity():
    base = datetime(2026, 9, 7, 9, tzinfo=UTC)
    rows = [
        {
            "user_id": "user-a",
            "project_id": "project-a",
            "task_id": None,
            "email": "a@example.test",
            "full_name": "A",
            "project": "Alpha",
            "task": None,
            "client": None,
            "started_at": base,
            "ended_at": base + timedelta(seconds=30),
            "seconds": 30,
            "time_type": "tracked",
            "status": "stopped",
            "activity_percent": 100,
            "keyboard_events": 4,
            "mouse_clicks": 2,
        },
        {
            "user_id": "user-b",
            "project_id": "project-a",
            "task_id": None,
            "email": "b@example.test",
            "full_name": "B",
            "project": "Alpha",
            "task": None,
            "client": None,
            "started_at": base + timedelta(minutes=1),
            "ended_at": base + timedelta(minutes=2, seconds=30),
            "seconds": 90,
            "time_type": "manual",
            "status": "approved",
            "activity_percent": 0,
            "keyboard_events": 1,
            "mouse_clicks": 3,
        },
    ]

    grouped = _group_custom_report_rows(rows, "project", True)

    assert len(grouped) == 1
    assert grouped[0]["project"] == "Alpha"
    assert grouped[0]["seconds"] == 120
    assert grouped[0]["activity_percent"] == 25
    assert grouped[0]["keyboard_events"] == 5
    assert grouped[0]["mouse_clicks"] == 5
    assert grouped[0]["email"] == "Multiple"
    assert grouped[0]["task"] == ""
    assert grouped[0]["time_type"] == "Multiple"
    assert _group_columns(["seconds", "project"], "project") == [
        "project",
        "seconds",
    ]


def test_screenshot_zip_respects_project_roles_and_reports_missing_objects(
    tmp_path, postgres_url
):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        owner = database.get_user_by_email("owner@example.test")
        worker = _member(database, owner, "worker@example.test")
        coworker = _member(database, owner, "coworker@example.test")
        outsider = _member(database, owner, "outsider@example.test")
        project = database.create_project("Export project", "", owner["id"])
        private_project = database.create_project("Private project", "", owner["id"])
        database.add_project_member(project["id"], worker["id"], "worker")
        database.add_project_member(project["id"], coworker["id"], "worker")
        database.add_project_member(private_project["id"], outsider["id"], "worker")
        worker_record = _capture(client, database, worker, project, "Worker editor")
        coworker_record = _capture(
            client, database, coworker, project, "Coworker editor"
        )

        _login(client, worker["email"])
        denied = client.get(
            f"/reports/screenshots.zip?project_id={private_project['id']}"
        )
        assert denied.status_code == 404
        scoped = client.get(f"/reports/screenshots.zip?project_id={project['id']}")
        assert scoped.status_code == 200
        with zipfile.ZipFile(io.BytesIO(scoped.content)) as archive:
            names = archive.namelist()
            manifest = archive.read("manifest.csv").decode()
        assert any(worker_record in name for name in names)
        assert not any(coworker_record in name for name in names)
        assert "Worker editor" in manifest
        assert "Coworker editor" not in manifest

        database.set_project_member_role(project["id"], worker["id"], "manager")
        visible = client.get(f"/reports/screenshots.zip?project_id={project['id']}")
        with zipfile.ZipFile(io.BytesIO(visible.content)) as archive:
            assert any(coworker_record in name for name in archive.namelist())

        missing = database.get_record(coworker_record)
        app.state.storage.delete(
            missing["screenshot_path"], missing.get("storage_version_id")
        )
        missing_export = client.get(
            f"/reports/screenshots.zip?project_id={project['id']}"
        )
        with zipfile.ZipFile(io.BytesIO(missing_export.content)) as archive:
            manifest = archive.read("manifest.csv").decode()
        assert coworker_record in manifest
        assert "missing" in manifest


def test_custom_report_filters_columns_and_keeps_saved_views_private(
    tmp_path, postgres_url, monkeypatch
):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        owner = database.get_user_by_email("owner@example.test")
        worker = _member(database, owner, "worker@example.test")
        coworker = _member(database, owner, "coworker@example.test")
        outsider = _member(database, owner, "outsider@example.test")
        project = database.create_project("Reporting project", "", owner["id"])
        database.add_project_member(project["id"], worker["id"], "worker")
        database.add_project_member(project["id"], coworker["id"], "worker")
        _capture(client, database, worker, project, "Worker editor")
        _capture(client, database, coworker, project, "Coworker editor")

        _login(client, worker["email"])
        page = client.get("/reports/custom")
        assert page.status_code == 200
        assert worker["email"] in page.text
        assert coworker["email"] not in page.text
        assert "Member email" in page.text

        exported = client.get(
            "/reports/custom.csv",
            params=[("columns", "email"), ("columns", "project")],
        )
        assert exported.status_code == 200
        assert exported.text.splitlines()[0] == "email,project"
        assert worker["email"] in exported.text
        assert coworker["email"] not in exported.text

        pdf = client.get(
            "/reports/custom.pdf",
            params=[("columns", "email"), ("columns", "project")],
        )
        assert pdf.status_code == 200
        assert pdf.headers["content-type"] == "application/pdf"
        assert pdf.headers["cache-control"] == "private, no-store"
        assert pdf.headers["content-disposition"].endswith('.pdf"')
        assert pdf.content.startswith(b"%PDF-")
        assert pdf.content.rstrip().endswith(b"%%EOF")

        def saturated_pdf(**_kwargs):
            raise PDFExportBusy("busy")

        monkeypatch.setattr("api.routers.reports.pdf_bytes", saturated_pdf)
        busy = client.get("/reports/custom.pdf")
        assert busy.status_code == 503
        assert busy.headers["retry-after"] == "2"

        saved = client.post(
            "/reports/custom/saved",
            data={
                "csrf": _csrf(page),
                "name": "My weekly view",
                "description": "Only my accessible work",
                "date_from": datetime.now(UTC).date().isoformat(),
                "date_to": datetime.now(UTC).date().isoformat(),
                "project_id": project["id"],
                "columns": ["email", "project", "seconds"],
                "group_by": "member",
                "collapsed": "true",
            },
            follow_redirects=False,
        )
        assert saved.status_code == 303
        saved_id = saved.headers["location"].split("=")[-1]
        saved_page = client.get(saved.headers["location"])
        assert saved_page.status_code == 200
        assert 'option value="member" selected' in saved_page.text
        assert 'name="collapsed" value="true" checked' in saved_page.text

        _login(client, outsider["email"])
        assert client.get(f"/reports/custom?saved_id={saved_id}").status_code == 404
        outsider_page = client.get("/reports/custom")
        assert (
            client.post(
                f"/reports/custom/saved/{saved_id}/delete",
                data={"csrf": _csrf(outsider_page)},
            ).status_code
            == 404
        )

        database.set_project_member_role(project["id"], worker["id"], "manager")
        _login(client, worker["email"])
        manager_page = client.get(f"/reports/custom?project_id={project['id']}")
        assert manager_page.status_code == 200
        assert coworker["email"] in manager_page.text

        grouped = client.get(
            "/reports/custom.csv",
            params=[
                ("project_id", project["id"]),
                ("group_by", "project"),
                ("collapsed", "true"),
                ("columns", "email"),
                ("columns", "seconds"),
            ],
        )
        assert grouped.status_code == 200
        grouped_rows = list(csv.DictReader(io.StringIO(grouped.text)))
        assert len(grouped_rows) == 1
        assert grouped_rows[0]["project"] == "Reporting project"
        assert grouped_rows[0]["email"] == "Multiple"
        assert int(grouped_rows[0]["seconds"]) > 0
        assert client.get("/reports/custom.csv?group_by=unsupported").status_code == 422


def test_specialized_reports_offer_bounded_audited_csv_and_pdf_exports(
    tmp_path, postgres_url, monkeypatch
):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        owner = database.get_user_by_email("owner@example.test")
        member = _member(database, owner, "report-member@example.test")
        _login(client, owner["email"], "correct horse battery staple")

        today = datetime.now(UTC).date()
        start = today - timedelta(days=6)
        params = {"date_from": start.isoformat(), "date_to": today.isoformat()}
        reports = client.get("/reports", params=params)
        assert reports.status_code == 200
        for slug in ("time", "activity", "attendance", "expenses"):
            assert (
                f"/reports/{slug}.csv?date_from={start}&amp;date_to={today}"
                in reports.text
            )
            assert (
                f"/reports/{slug}.pdf?date_from={start}&amp;date_to={today}"
                in reports.text
            )

            csv_export = client.get(f"/reports/{slug}.csv", params=params)
            assert csv_export.status_code == 200, csv_export.text
            assert csv_export.headers["content-type"].startswith("text/csv")
            assert csv_export.headers["cache-control"] == "private, no-store"
            assert csv_export.headers["x-content-type-options"] == "nosniff"
            assert str(start) in csv_export.headers["content-disposition"]

            pdf_export = client.get(f"/reports/{slug}.pdf", params=params)
            assert pdf_export.status_code == 200, pdf_export.text
            assert pdf_export.headers["content-type"] == "application/pdf"
            assert pdf_export.headers["cache-control"] == "private, no-store"
            assert pdf_export.headers["x-content-type-options"] == "nosniff"
            assert pdf_export.content.startswith(b"%PDF-")
            assert pdf_export.content.rstrip().endswith(b"%%EOF")

        with database.connect() as connection:
            audit_rows = connection.execute(
                """SELECT details FROM audit_events
                   WHERE actor_user_id=%s AND action='report.exported'
                   ORDER BY occurred_at DESC LIMIT 8""",
                (owner["id"],),
            ).fetchall()
        assert len(audit_rows) == 8
        details = "\n".join(row["details"] for row in audit_rows)
        assert "format=csv" in details
        assert "format=pdf" in details
        assert f"date_from={start};date_to={today}" in details

        reversed_period = client.get(
            "/reports/time.pdf",
            params={"date_from": today.isoformat(), "date_to": start.isoformat()},
        )
        assert reversed_period.status_code == 422
        oversized_period = client.get(
            "/reports/time.csv",
            params={
                "date_from": (today - timedelta(days=366)).isoformat(),
                "date_to": today.isoformat(),
            },
        )
        assert oversized_period.status_code == 422
        assert (
            client.get(
                "/reports/activity.pdf",
                params={**params, "project_id": str(uuid.uuid4())},
            ).status_code
            == 404
        )

        def saturated_pdf(**_kwargs):
            raise PDFExportBusy("busy")

        monkeypatch.setattr("api.routers.reports.pdf_bytes", saturated_pdf)
        busy = client.get("/reports/expenses.pdf", params=params)
        assert busy.status_code == 503
        assert busy.headers["retry-after"] == "2"
        monkeypatch.undo()

        monkeypatch.setattr(
            database,
            "time_report",
            lambda **_kwargs: [{}] * 10_001,
        )
        too_large = client.get("/reports/time.csv", params=params)
        assert too_large.status_code == 422
        assert "exceeds 10000 rows" in too_large.text

        _login(client, member["email"])
        for slug in ("time", "activity", "attendance", "expenses"):
            assert client.get(f"/reports/{slug}.csv", params=params).status_code == 403
            assert client.get(f"/reports/{slug}.pdf", params=params).status_code == 403


def test_scheduled_report_input_and_missing_ids_are_rejected(tmp_path, postgres_url):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        owner = database.get_user_by_email("owner@example.test")
        with pytest.raises(ValueError, match="report type"):
            database.create_scheduled_report(
                "Bad type", "unknown", "weekly", "owner@example.test", owner["id"]
            )
        with pytest.raises(ValueError, match="recipient"):
            database.create_scheduled_report(
                "Bad email", "time", "weekly", "not-an-email", owner["id"]
            )
        with pytest.raises(ValueError, match="delivery format"):
            database.create_scheduled_report(
                "Bad format",
                "time",
                "weekly",
                "owner@example.test",
                owner["id"],
                "spreadsheet",
            )
        with pytest.raises(ValueError, match="exact UTC minute"):
            database.create_scheduled_report(
                "Bad time",
                "time",
                "weekly",
                "owner@example.test",
                owner["id"],
                delivery_time=time(9, 0, 1),
            )
        with pytest.raises(ValueError, match="report range"):
            database.create_scheduled_report(
                "Bad range",
                "time",
                "weekly",
                "owner@example.test",
                owner["id"],
                range_preset="all_time",
            )
        with pytest.raises(ValueError, match="report name"):
            database.create_scheduled_report(
                "Header\r\nInjection",
                "time",
                "weekly",
                "owner@example.test",
                owner["id"],
            )

        page = client.get("/login")
        login = client.post(
            "/login",
            data={
                "email": owner["email"],
                "password": "correct horse battery staple",
                "csrf": _csrf(page),
            },
            follow_redirects=False,
        )
        assert login.status_code == 303
        reports = client.get("/reports")
        created = client.post(
            "/reports/scheduled",
            data={
                "csrf": _csrf(reports),
                "name": "Monthly PDF",
                "report_type": "time",
                "frequency": "monthly",
                "delivery_format": "pdf",
                "delivery_time": "14:30",
                "schedule_weekday": "4",
                "schedule_month_day": "-1",
                "range_preset": "previous_month",
                "recipients": "owner@example.test",
            },
            follow_redirects=False,
        )
        assert created.status_code == 303
        scheduled = database.list_scheduled_reports()
        assert scheduled[0]["delivery_format"] == "pdf"
        assert scheduled[0]["delivery_hour"] == 14
        assert scheduled[0]["delivery_minute"] == 30
        assert scheduled[0]["schedule_month_day"] == -1
        assert scheduled[0]["range_preset"] == "previous_month"
        next_at = datetime.fromisoformat(str(scheduled[0]["next_send_at"]))
        assert next_at.day == calendar.monthrange(next_at.year, next_at.month)[1]
        database.set_scheduled_report_enabled(scheduled[0]["id"], False)
        database.set_scheduled_report_enabled(scheduled[0]["id"], True)
        resumed = database.list_scheduled_reports()[0]
        assert resumed["enabled"] is True
        assert datetime.fromisoformat(str(resumed["next_send_at"])) > datetime.now(UTC)
        missing_id = str(uuid.uuid4())
        assert (
            client.post(
                f"/reports/scheduled/{missing_id}/delete",
                data={"csrf": _csrf(reports)},
            ).status_code
            == 404
        )


def test_scheduled_report_queries_are_date_bounded_and_failures_are_persisted(
    tmp_path, postgres_url
):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        owner = database.get_user_by_email("owner@example.test")
        member = _member(database, owner, "bounded@example.test")
        project = database.create_project("Bounded report", "", owner["id"])
        database.add_project_member(project["id"], member["id"], "worker")
        _capture(client, database, member, project, "Bounded editor")
        now = datetime.now(UTC)
        recent_start = now - timedelta(hours=1)
        recent_end = now + timedelta(hours=1)
        old_start = datetime(2020, 1, 1, tzinfo=UTC)
        old_end = datetime(2020, 1, 2, tzinfo=UTC)

        database.add_shift(
            member["id"],
            project["id"],
            now - timedelta(minutes=30),
            now + timedelta(minutes=30),
            "Current shift",
            owner["id"],
        )
        database.add_expense(
            member["id"],
            project["id"],
            now.date(),
            "software",
            Decimal("12.50"),
            "USD",
            "Current expense",
        )

        assert database.time_report(
            started_at=recent_start, ended_at=recent_end, limit=10
        )
        assert not database.time_report(
            started_at=old_start, ended_at=old_end, limit=10
        )
        assert database.activity_report(
            started_at=recent_start, ended_at=recent_end, limit=10
        )
        assert not database.activity_report(
            started_at=old_start, ended_at=old_end, limit=10
        )
        assert database.attendance_report(
            started_at=recent_start, ended_at=recent_end, limit=10
        )
        assert not database.attendance_report(
            started_at=old_start, ended_at=old_end, limit=10
        )
        assert database.list_expenses(
            incurred_from=now.date(),
            incurred_to=now.date() + timedelta(days=1),
            limit=10,
        )
        assert not database.list_expenses(
            incurred_from=old_start.date(), incurred_to=old_end.date(), limit=10
        )

        report_id = database.create_scheduled_report(
            "Failure state",
            "time",
            "daily",
            "owner@example.test",
            owner["id"],
            range_preset="previous_day",
        )
        retry_at = now + timedelta(minutes=5)
        database.mark_scheduled_report_failed(
            report_id, now, retry_at, "delivery_unavailable"
        )
        failed = next(
            report
            for report in database.list_scheduled_reports()
            if report["id"] == report_id
        )
        assert failed["consecutive_failures"] == 1
        assert failed["last_error_code"] == "delivery_unavailable"
        assert datetime.fromisoformat(str(failed["next_send_at"])) == retry_at

        database.mark_scheduled_report_sent(report_id, now, now + timedelta(days=1))
        recovered = next(
            report
            for report in database.list_scheduled_reports()
            if report["id"] == report_id
        )
        assert recovered["consecutive_failures"] == 0
        assert recovered["last_error_code"] == ""


def test_scheduled_reports_are_atomically_claimed_across_workers(
    tmp_path, postgres_url
):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app):
        database = app.state.database
        owner = database.get_user_by_email("owner@example.test")
        report_ids = {
            database.create_scheduled_report(
                name,
                "time",
                "daily",
                "owner@example.test",
                owner["id"],
            )
            for name in ("Claim A", "Claim B")
        }
        now = datetime.now(UTC)
        with database.connect() as connection:
            connection.execute(
                "UPDATE scheduled_reports SET next_send_at=%s WHERE id=ANY(%s::uuid[])",
                (now - timedelta(minutes=1), list(report_ids)),
            )

        first_token = str(uuid.uuid4())
        second_token = str(uuid.uuid4())
        first = database.due_scheduled_reports(
            now, first_token, lease_seconds=60, limit=1
        )
        second = database.due_scheduled_reports(
            now, second_token, lease_seconds=60, limit=1
        )

        assert {first[0]["id"], second[0]["id"]} == report_ids
        assert not database.due_scheduled_reports(
            now, str(uuid.uuid4()), lease_seconds=60
        )
        reclaimed_token = str(uuid.uuid4())
        reclaimed = database.due_scheduled_reports(
            now + timedelta(seconds=61),
            reclaimed_token,
            lease_seconds=60,
        )
        assert {report["id"] for report in reclaimed} == report_ids
        with pytest.raises(ValueError, match="claim was lost"):
            database.mark_scheduled_report_failed(
                reclaimed[0]["id"],
                now,
                now + timedelta(minutes=5),
                "delivery_unavailable",
                first_token,
            )

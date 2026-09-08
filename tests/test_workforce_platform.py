import hashlib
import hmac
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from api.config import Settings
from api.security import hash_password
from api.services.payments import PayrollDeliveryService


def _team(database):
    admin = database.bootstrap_admin(
        "owner@example.test", hash_password("owner password long")
    )
    _, token = database.create_invitation("member@example.test", admin["id"], 24)
    member = database.accept_invitation(token, hash_password("member password long"))
    project = database.create_project("Client portal", "Build the portal", admin["id"])
    database.add_project_member(project["id"], member["id"])
    return admin, member, project


def test_workforce_approval_and_finance_workflows(database):
    admin, member, project = _team(database)
    started = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    entry_id = database.add_manual_time(
        member["id"],
        project["id"],
        None,
        started,
        started + timedelta(hours=2),
        "Design review",
    )
    database.review_item("manual_time_entries", entry_id, admin["id"], "approved")
    assert database.list_manual_time(member["id"])[0]["status"] == "approved"

    leave_id = database.add_time_off(
        member["id"], "paid", date(2026, 9, 4), date(2026, 9, 4), 480, "Family day"
    )
    database.review_item("time_off_requests", leave_id, admin["id"], "approved")
    assert database.list_time_off(member["id"])[0]["status"] == "approved"

    expense_id = database.add_expense(
        member["id"],
        project["id"],
        date(2026, 9, 2),
        "Software",
        Decimal("19.99"),
        "USD",
        "Test service",
    )
    database.review_item("expenses", expense_id, admin["id"], "approved")
    assert database.list_expenses(member["id"])[0]["amount"] == Decimal("19.99")

    client_id = database.create_client("Acme", "billing@acme.test")
    database.create_invoice(
        client_id,
        date(2026, 9, 1),
        date(2026, 9, 30),
        "Consulting",
        Decimal("2"),
        Decimal("125"),
        "USD",
        admin["id"],
    )
    database.set_user_profile(
        member["id"], "member", "Alex", Decimal("40"), Decimal("90"), 2400
    )
    database.create_payroll(member["id"], date(2026, 9, 1), date(2026, 9, 7), "USD")
    finance = database.finance_summary()
    assert finance["invoices"][0]["subtotal"] == Decimal("250")
    assert finance["payroll_runs"][0]["gross_amount"] == Decimal("80.00")
    # The scalar gross-paid total must survive alongside the run list;
    # both used to share the "payroll" key and the list won.
    assert not isinstance(finance["payroll"], list)


def test_agent_can_switch_between_assigned_projects(database):
    admin, member, first = _team(database)
    second = database.create_project("Mobile app", "", admin["id"])
    database.add_project_member(second["id"], member["id"])
    device, _ = database.create_device("Workstation", member["id"], first["id"])

    first_session = database.sync_work_session(device, "active", None, first["id"])
    second_session = database.sync_work_session(device, "active", None, second["id"])

    assert first_session["project_id"] == first["id"]
    assert second_session["project_id"] == second["id"]
    assert first_session["id"] != second_session["id"]


def test_dashboard_counts_segments_that_cross_the_week_boundary(database):
    admin, member, project = _team(database)
    device, _ = database.create_device("Workstation", member["id"], project["id"])
    session = database.sync_work_session(device, "active", None, project["id"])

    with database.connect() as connection:
        connection.execute(
            """UPDATE work_session_segments
                  SET started_at = date_trunc('week', CURRENT_TIMESTAMP) - INTERVAL '2 days'
                WHERE session_id = %s""",
            (session["id"],),
        )

    summary = database.dashboard_summary()
    assert summary["tracked_seconds"] > 0
    assert summary["active_members"] == 1
    assert summary["tracked_members"] == 1
    assert sum(day["tracked_seconds"] for day in summary["daily"]) > 0
    project_summary = next(
        item for item in summary["projects"] if item["id"] == project["id"]
    )
    assert project_summary["tracked_seconds"] > 0
    assert project_summary["member_count"] == 1


def test_tracking_policy_round_trip(database):
    values = {
        "name": "Dayfinch Labs",
        "timezone": "Asia/Karachi",
        "currency": "PKR",
        "screenshot_frequency": 3,
        "screenshot_blur": True,
        "track_apps": True,
        "track_urls": False,
        "allow_manual_time": True,
        "require_time_approval": True,
        "allow_screenshot_delete": False,
        "idle_timeout_minutes": 15,
        "retention_days": 365,
    }
    database.update_organization_settings(values)
    saved = database.organization_settings()
    for key, value in values.items():
        assert saved[key] == value


def test_signed_payroll_dispatch_and_callback(database, monkeypatch):
    admin, member, _ = _team(database)
    payment_id = database.create_payroll(
        member["id"], date(2026, 9, 1), date(2026, 9, 7), "USD"
    )
    settings = Settings(
        data_dir=Path("/tmp/dayfinch-payment-test"),
        admin_password="test-password",
        session_secret="test-secret",
        cookie_secure=False,
        max_upload_bytes=1024,
        retention_days=30,
        payment_webhook_url="https://payments.example.test/payroll",
        payment_webhook_secret="provider-shared-secret",
    )
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        @staticmethod
        def read(_limit):
            return b'{"reference":"provider-42","status":"processing"}'

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("api.services.payments.urlopen", fake_urlopen)
    service = PayrollDeliveryService(database, settings)
    sent = service.send(payment_id)
    assert sent["status"] == "processing"
    assert sent["external_reference"] == "provider-42"
    assert captured["request"].get_header("Idempotency-key") == payment_id

    callback = json.dumps(
        {"payment_id": payment_id, "status": "paid", "reference": "provider-42"},
        separators=(",", ":"),
    ).encode()
    signature = (
        "sha256="
        + hmac.new(
            settings.payment_webhook_secret.encode(), callback, hashlib.sha256
        ).hexdigest()
    )
    reconciled = service.handle_callback(callback, signature)
    assert reconciled["status"] == "paid"
    assert reconciled["paid_at"] is not None
    assert database.get_user(admin["id"])["role"] == "admin"

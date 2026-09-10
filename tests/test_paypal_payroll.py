import base64
import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from api.config import Settings
from api.main import create_app
from api.security import hash_password
from api.services.payments import PaymentDeliveryError, PayrollDeliveryService


def _payroll(database):
    owner = database.bootstrap_admin(
        "owner@example.test", hash_password("owner password long enough")
    )
    _, token = database.create_invitation("member@example.test", owner["id"], 24)
    member = database.accept_invitation(
        token, hash_password("member password long enough")
    )
    project = database.create_project("Payroll project", "", owner["id"])
    database.add_project_member(project["id"], member["id"])
    database.set_user_profile(
        member["id"],
        "member",
        "Payroll Member",
        Decimal("30"),
        Decimal("60"),
        2400,
    )
    started = datetime(2026, 9, 1, 9, tzinfo=UTC)
    entry = database.add_manual_time(
        member["id"],
        project["id"],
        None,
        started,
        started + timedelta(hours=1),
        "Approved work",
    )
    database.review_item("manual_time_entries", entry, owner["id"], "approved")
    timesheet = database.submit_timesheet(
        member["id"], date(2026, 9, 1), date(2026, 9, 7)
    )
    database.review_timesheet(timesheet["id"], owner["id"], "approved", "")
    payment_id = database.create_payroll(
        member["id"], date(2026, 9, 1), date(2026, 9, 7), "USD"
    )
    return owner, member, payment_id


def _settings() -> Settings:
    return Settings(
        data_dir=Path("/tmp/dayfinch-paypal-test"),
        admin_password="test-password",
        session_secret="test-secret",
        cookie_secure=False,
        max_upload_bytes=1024,
        retention_days=30,
        payment_provider="paypal",
        paypal_client_id="paypal-client",
        paypal_client_secret="paypal-secret-do-not-leak",
        paypal_api_url="https://api-m.sandbox.paypal.com",
    )


def _csrf(response) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def _login(client: TestClient, email: str, password: str) -> None:
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf": _csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _token_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": "provider-access-token-long-enough",
            "token_type": "Bearer",
            "expires_in": 3600,
        },
    )


def test_paypal_dispatch_snapshots_recipient_and_reconciles(database):
    owner, member, payment_id = _payroll(database)
    database.set_payroll_destination(
        member["id"], "paypal", "first@example.test", owner["id"]
    )
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/oauth2/token":
            expected = base64.b64encode(b"paypal-client:paypal-secret-do-not-leak")
            assert request.headers["Authorization"] == f"Basic {expected.decode()}"
            return _token_response()
        if request.method == "POST":
            payload = json.loads(request.content)
            assert request.headers["PayPal-Request-Id"] == payment_id
            assert payload["sender_batch_header"]["sender_batch_id"] == payment_id
            assert payload["items"][0]["sender_item_id"] == payment_id
            assert payload["items"][0]["receiver"] == "first@example.test"
            assert payload["items"][0]["amount"] == {
                "value": "30.00",
                "currency": "USD",
            }
            return httpx.Response(
                201,
                json={
                    "batch_header": {
                        "payout_batch_id": "BATCH-123",
                        "batch_status": "PENDING",
                    }
                },
            )
        assert request.url.path == "/v1/payments/payouts/BATCH-123"
        return httpx.Response(
            200,
            json={
                "batch_header": {
                    "payout_batch_id": "BATCH-123",
                    "batch_status": "SUCCESS",
                },
                "items": [{"transaction_status": "SUCCESS"}],
            },
        )

    service = PayrollDeliveryService(
        database, _settings(), transport=httpx.MockTransport(handler)
    )
    try:
        sent = service.send(payment_id)
        assert sent["status"] == "processing"
        assert sent["provider"] == "paypal"
        assert sent["recipient"] == "first@example.test"
        assert sent["external_reference"] == "BATCH-123"

        database.set_payroll_destination(
            member["id"], "paypal", "changed@example.test", owner["id"]
        )
        with pytest.raises(PaymentDeliveryError, match="Only draft or failed"):
            service.send(payment_id)
        reconciled = service.reconcile(payment_id)
        assert reconciled["status"] == "paid"
        assert reconciled["recipient"] == "first@example.test"
        assert reconciled["paid_at"] is not None
        assert sum(request.method == "POST" for request in requests) == 2
    finally:
        service.close()


def test_paypal_requires_explicit_confirmed_destination(database):
    _, _, payment_id = _payroll(database)
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return _token_response()

    service = PayrollDeliveryService(
        database, _settings(), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(PaymentDeliveryError, match="Confirm this member"):
            service.send(payment_id)
        assert called is False
        assert database.get_payroll_payment(payment_id)["status"] == "draft"
    finally:
        service.close()


def test_payroll_requires_approved_period_and_rejects_duplicate(database):
    owner = database.bootstrap_admin("owner@example.test", "hash")
    _, token = database.create_invitation("worker@example.test", owner["id"], 24)
    member = database.accept_invitation(token, "hash")
    project = database.create_project("Locked payroll", "", owner["id"])
    database.add_project_member(project["id"], member["id"])
    database.set_user_profile(
        member["id"], "member", "Worker", Decimal("20"), Decimal("40"), 2400
    )
    started = datetime(2026, 8, 3, 9, tzinfo=UTC)
    entry = database.add_manual_time(
        member["id"], project["id"], None, started, started + timedelta(hours=1), ""
    )
    database.review_item("manual_time_entries", entry, owner["id"], "approved")

    with pytest.raises(ValueError, match="Approve"):
        database.create_payroll(member["id"], date(2026, 8, 3), date(2026, 8, 9), "USD")
    sheet = database.submit_timesheet(member["id"], date(2026, 8, 3), date(2026, 8, 9))
    database.review_timesheet(sheet["id"], owner["id"], "approved", "")
    payment_id = database.create_payroll(
        member["id"], date(2026, 8, 3), date(2026, 8, 9), "USD"
    )
    assert (
        database.get_payroll_payment(payment_id)["source_timesheet_id"] == sheet["id"]
    )
    with pytest.raises(ValueError, match="overlapping payroll"):
        database.create_payroll(member["id"], date(2026, 8, 3), date(2026, 8, 9), "USD")
    with pytest.raises(ValueError, match="locked"):
        database.add_manual_time(
            member["id"],
            project["id"],
            None,
            started + timedelta(hours=2),
            started + timedelta(hours=3),
            "late edit",
        )


def test_payroll_overtime_is_calculated_per_configured_week(database):
    owner = database.bootstrap_admin("owner@example.test", "hash")
    _, token = database.create_invitation("worker@example.test", owner["id"], 24)
    member = database.accept_invitation(token, "hash")
    project = database.create_project("Overtime payroll", "", owner["id"])
    database.add_project_member(project["id"], member["id"])
    database.set_user_profile(
        member["id"], "member", "Worker", Decimal("10"), Decimal("20"), 2400
    )
    database.update_organization_settings(
        {
            "timezone": "UTC",
            "overtime_enabled": True,
            "weekly_overtime_minutes": 2400,
            "overtime_multiplier": "1.5",
        }
    )
    for day in range(17, 22):
        started = datetime(2026, 8, day, 8, tzinfo=UTC)
        entry = database.add_manual_time(
            member["id"],
            project["id"],
            None,
            started,
            started + timedelta(hours=9),
            "Week one",
        )
        database.review_item("manual_time_entries", entry, owner["id"], "approved")
    started = datetime(2026, 8, 24, 8, tzinfo=UTC)
    entry = database.add_manual_time(
        member["id"],
        project["id"],
        None,
        started,
        started + timedelta(hours=5),
        "Week two",
    )
    database.review_item("manual_time_entries", entry, owner["id"], "approved")
    sheet = database.submit_timesheet(
        member["id"], date(2026, 8, 17), date(2026, 8, 30)
    )
    database.review_timesheet(sheet["id"], owner["id"], "approved", "")

    payment_id = database.create_payroll(
        member["id"], date(2026, 8, 17), date(2026, 8, 30), "USD"
    )
    payment = database.get_payroll_payment(payment_id)
    assert payment["regular_minutes"] == 45 * 60
    assert payment["overtime_minutes"] == 5 * 60
    assert payment["pay_rate_snapshot"] == Decimal("10.00")
    assert payment["overtime_multiplier_snapshot"] == Decimal("1.50")
    assert payment["gross_amount"] == Decimal("525.00")


def test_paypal_failure_is_safe_and_retry_keeps_original_recipient(database):
    owner, member, payment_id = _payroll(database)
    database.set_payroll_destination(
        member["id"], "paypal", "original@example.test", owner["id"]
    )
    payout_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal payout_calls
        if request.url.path == "/v1/oauth2/token":
            return _token_response()
        payout_calls += 1
        if payout_calls == 1:
            return httpx.Response(
                500,
                text="provider secret body paypal-secret-do-not-leak",
            )
        payload = json.loads(request.content)
        assert payload["items"][0]["receiver"] == "original@example.test"
        return httpx.Response(
            201,
            json={
                "batch_header": {
                    "payout_batch_id": "RETRY-BATCH",
                    "batch_status": "PENDING",
                }
            },
        )

    service = PayrollDeliveryService(
        database, _settings(), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(PaymentDeliveryError, match="paypal_payout_http_500"):
            service.send(payment_id)
        failed = database.get_payroll_payment(payment_id)
        assert failed["status"] == "failed"
        assert failed["failure_reason"] == "paypal_payout_http_500"
        assert "secret" not in failed["failure_reason"]

        database.set_payroll_destination(
            member["id"], "paypal", "attacker@example.test", owner["id"]
        )
        retried = service.send(payment_id)
        assert retried["status"] == "processing"
        assert retried["recipient"] == "original@example.test"
    finally:
        service.close()


def test_paypal_rejects_unsupported_currency_before_network(database):
    owner, member, payment_id = _payroll(database)
    database.set_payroll_destination(
        member["id"], "paypal", "member@example.test", owner["id"]
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE payroll_payments SET currency='PKR' WHERE id=%s", (payment_id,)
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return _token_response()
        raise AssertionError("Unsupported currency must not reach the payout endpoint")

    service = PayrollDeliveryService(
        database, _settings(), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(PaymentDeliveryError, match="unsupported_currency"):
            service.send(payment_id)
        assert database.get_payroll_payment(payment_id)["failure_reason"] == (
            "paypal_unsupported_currency"
        )
    finally:
        service.close()


def test_paypal_retry_stops_before_duplicate_protection_expires(database):
    owner, member, payment_id = _payroll(database)
    database.set_payroll_destination(
        member["id"], "paypal", "member@example.test", owner["id"]
    )
    database.claim_payroll_delivery(payment_id, "paypal")
    database.record_payroll_delivery(
        payment_id, "failed", provider="paypal", failure_reason="paypal_api_unavailable"
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE payroll_payments SET delivery_started_at=%s WHERE id=%s",
            (datetime.now(UTC) - timedelta(days=30), payment_id),
        )
    with pytest.raises(ValueError, match="duplicate-protection window expired"):
        database.claim_payroll_delivery(payment_id, "paypal")


def test_interrupted_dispatch_claim_becomes_retryable(database):
    owner, member, payment_id = _payroll(database)
    database.set_payroll_destination(
        member["id"], "paypal", "member@example.test", owner["id"]
    )
    database.claim_payroll_delivery(payment_id, "paypal")
    with database.connect() as connection:
        connection.execute(
            "UPDATE payroll_payments SET updated_at=%s WHERE id=%s",
            (datetime.now(UTC) - timedelta(minutes=11), payment_id),
        )
    assert database.recover_stale_payroll_claims("paypal") == 1
    failed = database.get_payroll_payment(payment_id)
    assert failed["status"] == "failed"
    assert failed["failure_reason"] == "payroll_dispatch_interrupted"
    retried = database.claim_payroll_delivery(payment_id, "paypal")
    assert retried["status"] == "processing"
    assert retried["recipient"] == "member@example.test"


def test_paypal_duplicate_response_recovers_original_batch(database):
    owner, member, payment_id = _payroll(database)
    database.set_payroll_destination(
        member["id"], "paypal", "member@example.test", owner["id"]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return _token_response()
        return httpx.Response(
            400,
            json={
                "name": "DUPLICATE_BATCH_ID",
                "links": [
                    {
                        "rel": "self",
                        "href": (
                            "https://api-m.sandbox.paypal.com/v1/payments/"
                            "payouts/ORIGINAL-BATCH"
                        ),
                    }
                ],
            },
        )

    service = PayrollDeliveryService(
        database, _settings(), transport=httpx.MockTransport(handler)
    )
    try:
        payment = service.send(payment_id)
        assert payment["status"] == "processing"
        assert payment["external_reference"] == "ORIGINAL-BATCH"
    finally:
        service.close()


def test_paypal_destination_route_requires_owner_and_confirmation(
    tmp_path, postgres_url
):
    settings = Settings(
        data_dir=tmp_path,
        admin_password="owner password long enough",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024,
        retention_days=30,
        environment="test",
        admin_email="owner@example.test",
        database_url=postgres_url,
        payment_provider="paypal",
        paypal_client_id="client-id",
        paypal_client_secret="client-secret",
    )

    def no_provider_call(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Destination management must not call PayPal")

    app = create_app(settings, payment_transport=httpx.MockTransport(no_provider_call))
    with TestClient(app) as client:
        database = app.state.database
        owner = database.get_user_by_email("owner@example.test")
        _, token = database.create_invitation("member@example.test", owner["id"], 24)
        member = database.accept_invitation(
            token, hash_password("member password long enough")
        )
        _, token = database.create_invitation("manager@example.test", owner["id"], 24)
        manager = database.accept_invitation(
            token, hash_password("manager password long enough")
        )
        database.set_user_profile(
            manager["id"], "manager", "Manager", Decimal("0"), Decimal("0"), 0
        )

        _login(client, owner["email"], "owner password long enough")
        people = client.get("/people")
        assert "PayPal payout email" in people.text
        assert 'value="member@example.test"' not in people.text
        unconfirmed = client.post(
            f"/people/{member['id']}/payroll-destination",
            data={
                "provider": "paypal",
                "recipient": "payout@example.test",
                "csrf": _csrf(people),
            },
        )
        assert unconfirmed.status_code == 422
        confirmed = client.post(
            f"/people/{member['id']}/payroll-destination",
            data={
                "provider": "paypal",
                "recipient": "payout@example.test",
                "confirmed": "true",
                "csrf": _csrf(people),
            },
            follow_redirects=False,
        )
        assert confirmed.status_code == 303
        assert (
            database.payroll_destinations("paypal")[member["id"]]["recipient"]
            == "payout@example.test"
        )

        _login(client, manager["email"], "manager password long enough")
        denied = client.post(
            f"/people/{member['id']}/payroll-destination",
            data={
                "provider": "paypal",
                "recipient": "attacker@example.test",
                "confirmed": "true",
                "csrf": _csrf(client.get("/people")),
            },
        )
        assert denied.status_code == 403


def test_provider_managed_paid_payroll_cannot_be_downgraded(database):
    owner, member, payment_id = _payroll(database)
    database.set_payroll_destination(
        member["id"], "paypal", "member@example.test", owner["id"]
    )
    claimed = database.claim_payroll_delivery(payment_id, "paypal")
    assert claimed["recipient"] == "member@example.test"
    database.record_payroll_delivery(
        payment_id,
        "paid",
        provider="paypal",
        external_reference="PAID-BATCH",
    )
    with pytest.raises(ValueError, match="Paid payroll"):
        database.record_payroll_delivery(
            payment_id,
            "failed",
            provider="paypal",
            external_reference="PAID-BATCH",
        )
    with pytest.raises(ValueError, match="Provider-managed"):
        database.set_financial_status("payroll_payments", payment_id, "failed")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"batch_header": {"batch_status": "PENDING"}}, ("processing", "")),
        (
            {"items": [{"transaction_status": "UNCLAIMED"}]},
            ("processing", ""),
        ),
        (
            {"items": [{"transaction_status": "RETURNED"}]},
            ("failed", "paypal_returned"),
        ),
        (
            {"items": [{"transaction_status": "SUCCESS"}]},
            ("paid", ""),
        ),
        (
            {"batch_header": {"batch_status": "DENIED"}},
            ("failed", "paypal_denied"),
        ),
    ],
)
def test_paypal_status_mapping_is_fail_closed(payload, expected):
    assert PayrollDeliveryService._paypal_status(payload) == expected

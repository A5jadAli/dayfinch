import base64
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
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
    project = database.create_project("Wise payroll", "", owner["id"])
    database.add_project_member(project["id"], member["id"])
    database.set_user_profile(
        member["id"], "member", "Wise Member", Decimal("30"), Decimal("60"), 2400
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
    sheet = database.submit_timesheet(member["id"], date(2026, 9, 1), date(2026, 9, 7))
    database.review_timesheet(sheet["id"], owner["id"], "approved", "")
    payment_id = database.create_payroll(
        member["id"], date(2026, 9, 1), date(2026, 9, 7), "USD"
    )
    return owner, member, payment_id


def _settings(*, webhook_public_key_b64: str = "") -> Settings:
    return Settings(
        data_dir=Path("/tmp/dayfinch-wise-test"),
        admin_password="test-password",
        session_secret="test-secret",
        cookie_secure=False,
        max_upload_bytes=1024,
        retention_days=30,
        payment_provider="wise",
        wise_api_token="wise-api-token-long-enough",
        wise_profile_id=101,
        wise_balance_id=202,
        wise_source_currency="GBP",
        wise_api_url="https://api.wise-sandbox.com/2026Q3",
        wise_webhook_public_key_b64=webhook_public_key_b64,
    )


def _webhook_key_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_key, base64.b64encode(public_pem).decode()


def _signed_webhook(private_key, payload: dict) -> tuple[bytes, str]:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = private_key.sign(body, padding.PKCS1v15(), hashes.SHA256())
    return body, base64.b64encode(signature).decode()


def _state_event(
    transfer_id: int,
    state: str,
    occurred_at: datetime,
    *,
    profile_id: int = 101,
) -> dict:
    return {
        "data": {
            "resource": {
                "type": "transfer",
                "id": transfer_id,
                "profile_id": profile_id,
                "account_id": 202,
            },
            "current_state": state,
            "previous_state": "processing",
            "occurred_at": occurred_at.isoformat(),
        },
        "subscription_id": str(uuid.uuid4()),
        "event_type": "transfers#state-change",
        "schema_version": "4.0.0",
        "sent_at": datetime.now(UTC).isoformat(),
    }


def _csrf(response) -> str:
    marker = 'name="csrf" value="'
    assert marker in response.text
    return response.text.split(marker, 1)[1].split('"', 1)[0]


def _login(client: TestClient, email: str, password: str) -> None:
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf": _csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_wise_quote_transfer_funding_and_reconciliation(database):
    owner, member, payment_id = _payroll(database)
    database.set_payroll_destination(
        member["id"], "wise", "8692237", owner["id"], "USD"
    )
    calls = []
    quote_id = "8fa9be20-ba43-4b15-abbb-9424e1481050"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer wise-api-token-long-enough"
        assert request.headers["X-External-Correlation-Id"] == payment_id
        if request.url.path.endswith("/profiles/101/quotes"):
            payload = json.loads(request.content)
            assert payload == {
                "sourceCurrency": "GBP",
                "targetCurrency": "USD",
                "targetAmount": 30.0,
                "targetAccount": 8692237,
                "payOut": None,
                "preferredPayIn": "BALANCE",
            }
            return httpx.Response(
                200,
                json={
                    "id": quote_id,
                    "targetCurrency": "USD",
                    "targetAmount": 30,
                    "targetAccount": 8692237,
                },
            )
        if request.url.path.endswith("/transfers"):
            payload = json.loads(request.content)
            assert payload["quoteUuid"] == quote_id
            assert payload["customerTransactionId"] == payment_id
            return httpx.Response(
                201,
                json={
                    "id": 16521632,
                    "targetAccount": 8692237,
                    "customerTransactionId": payment_id,
                    "status": "incoming_payment_waiting",
                },
            )
        if request.method == "POST":
            assert request.url.path.endswith(
                "/profiles/101/transfers/16521632/payments"
            )
            assert json.loads(request.content) == {"type": "BALANCE", "balanceId": 202}
            return httpx.Response(201, json={"type": "BALANCE", "status": "COMPLETED"})
        assert request.url.path.endswith("/transfers/16521632")
        return httpx.Response(
            200,
            json={"id": 16521632, "status": "outgoing_payment_sent"},
        )

    service = PayrollDeliveryService(
        database, _settings(), transport=httpx.MockTransport(handler)
    )
    try:
        sent = service.send(payment_id)
        assert sent["status"] == "processing"
        assert sent["provider"] == "wise"
        assert sent["recipient"] == "8692237"
        assert sent["recipient_currency"] == "USD"
        assert sent["external_reference"] == "16521632"
        reconciled = service.reconcile(payment_id)
        assert reconciled["status"] == "paid"
        assert reconciled["paid_at"] is not None
        assert len(calls) == 4
    finally:
        service.close()


def test_wise_funding_retry_reuses_idempotent_transfer(database):
    owner, member, payment_id = _payroll(database)
    database.set_payroll_destination(
        member["id"], "wise", "8692237", owner["id"], "USD"
    )
    quote_id = "8fa9be20-ba43-4b15-abbb-9424e1481050"
    quote_calls = transfer_calls = funding_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal quote_calls, transfer_calls, funding_calls
        if request.url.path.endswith("/profiles/101/quotes"):
            quote_calls += 1
            return httpx.Response(
                200,
                json={
                    "id": quote_id,
                    "targetCurrency": "USD",
                    "targetAmount": 30,
                    "targetAccount": 8692237,
                },
            )
        if request.url.path.endswith("/transfers"):
            transfer_calls += 1
            return httpx.Response(
                201,
                json={
                    "id": 16521632,
                    "targetAccount": 8692237,
                    "customerTransactionId": payment_id,
                },
            )
        funding_calls += 1
        if funding_calls == 1:
            return httpx.Response(500, text="private provider failure details")
        return httpx.Response(201, json={"status": "COMPLETED"})

    service = PayrollDeliveryService(
        database, _settings(), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(PaymentDeliveryError, match="wise_funding_http_500"):
            service.send(payment_id)
        failed = database.get_payroll_payment(payment_id)
        assert failed["status"] == "failed"
        assert failed["external_reference"] == "16521632"
        assert failed["failure_reason"] == "wise_funding_http_500"

        retried = service.send(payment_id)
        assert retried["status"] == "processing"
        assert quote_calls == 1
        assert transfer_calls == 1
        assert funding_calls == 2
    finally:
        service.close()


def test_wise_destination_currency_must_match_payroll(database):
    owner, member, payment_id = _payroll(database)
    database.set_payroll_destination(
        member["id"], "wise", "8692237", owner["id"], "PKR"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("A destination mismatch must not call Wise")

    service = PayrollDeliveryService(
        database, _settings(), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(PaymentDeliveryError, match="currency_mismatch"):
            service.send(payment_id)
        assert database.get_payroll_payment(payment_id)["failure_reason"] == (
            "wise_recipient_currency_mismatch"
        )
    finally:
        service.close()


@pytest.mark.parametrize(
    ("provider_status", "expected"),
    [
        ("incoming_payment_waiting", ("processing", "")),
        ("processing", ("processing", "")),
        ("funds_converted", ("processing", "")),
        ("outgoing_payment_sent", ("paid", "")),
        ("bounced_back", ("processing", "")),
        ("cancelled", ("failed", "wise_cancelled")),
        ("funds_refunded", ("failed", "wise_funds_refunded")),
        ("charged_back", ("failed", "wise_charged_back")),
        ("new_provider_state", ("processing", "")),
    ],
)
def test_wise_status_mapping_is_fail_closed(provider_status, expected):
    assert PayrollDeliveryService._wise_status({"status": provider_status}) == expected


def test_paid_wise_transfer_is_followed_up_and_reversal_removes_paid_total(database):
    owner, member, payment_id = _payroll(database)
    database.set_payroll_destination(
        member["id"], "wise", "8692237", owner["id"], "USD"
    )
    database.claim_payroll_delivery(payment_id, "wise")
    database.record_payroll_delivery(
        payment_id,
        "paid",
        provider="wise",
        external_reference="16521632",
    )
    originally_paid = database.get_payroll_payment(payment_id)
    assert originally_paid["paid_at"]
    assert database.finance_summary()["payroll"] == Decimal("30.00")
    with database.connect() as connection:
        connection.execute(
            "UPDATE payroll_payments SET next_reconcile_at=%s WHERE id=%s",
            (datetime.now(UTC) - timedelta(minutes=1), payment_id),
        )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.endswith("/transfers/16521632")
        return httpx.Response(
            200,
            json={"id": 16521632, "status": "funds_refunded"},
        )

    service = PayrollDeliveryService(
        database, _settings(), transport=httpx.MockTransport(handler)
    )
    try:
        assert service.reconcile_due() == 1
    finally:
        service.close()
    reversed_payment = database.get_payroll_payment(payment_id)
    assert reversed_payment["status"] == "reversed"
    assert reversed_payment["failure_reason"] == "wise_funds_refunded"
    assert reversed_payment["reversed_at"]
    assert reversed_payment["paid_at"] == originally_paid["paid_at"]
    assert reversed_payment["next_reconcile_at"] is None
    assert database.finance_summary()["payroll"] == Decimal("0")
    assert database.reconcilable_payroll_payments("wise") == []
    with database.connect() as connection:
        audit = connection.execute(
            """SELECT actor_user_id,action,details FROM audit_events
               WHERE target_type='payroll' AND target_id=%s""",
            (payment_id,),
        ).fetchone()
    assert audit == {
        "actor_user_id": None,
        "action": "payroll.provider_reversed",
        "details": "wise:wise_funds_refunded",
    }


def test_wise_bounce_after_payment_returns_to_reconciliation(database):
    owner, member, payment_id = _payroll(database)
    database.set_payroll_destination(
        member["id"], "wise", "8692237", owner["id"], "USD"
    )
    database.claim_payroll_delivery(payment_id, "wise")
    database.record_payroll_delivery(
        payment_id,
        "paid",
        provider="wise",
        external_reference="16521632",
    )

    responses = iter(["bounced_back", "outgoing_payment_sent"])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": 16521632, "status": next(responses)},
        )

    service = PayrollDeliveryService(
        database, _settings(), transport=httpx.MockTransport(handler)
    )
    try:
        bouncing = service.reconcile(payment_id)
        assert bouncing["status"] == "processing"
        assert bouncing["paid_at"] is not None
        resent = service.reconcile(payment_id)
        assert resent["status"] == "paid"
        assert resent["reconcile_until"] is not None
    finally:
        service.close()


def test_wise_paid_follow_up_expires_without_provider_call(database):
    owner, member, payment_id = _payroll(database)
    database.set_payroll_destination(
        member["id"], "wise", "8692237", owner["id"], "USD"
    )
    database.claim_payroll_delivery(payment_id, "wise")
    database.record_payroll_delivery(
        payment_id,
        "paid",
        provider="wise",
        external_reference="16521632",
    )
    with database.connect() as connection:
        connection.execute(
            """UPDATE payroll_payments
               SET next_reconcile_at=%s,reconcile_until=%s WHERE id=%s""",
            (
                datetime.now(UTC) - timedelta(days=2),
                datetime.now(UTC) - timedelta(days=1),
                payment_id,
            ),
        )
    assert database.reconcilable_payroll_payments("wise") == []


def test_wise_reconciliation_outage_uses_durable_bounded_backoff(database):
    owner, member, payment_id = _payroll(database)
    database.set_payroll_destination(
        member["id"], "wise", "8692237", owner["id"], "USD"
    )
    database.claim_payroll_delivery(payment_id, "wise")
    database.record_payroll_delivery(
        payment_id,
        "processing",
        provider="wise",
        external_reference="16521632",
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE payroll_payments SET next_reconcile_at=%s WHERE id=%s",
            (datetime.now(UTC) - timedelta(minutes=1), payment_id),
        )

    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="private outage details")

    service = PayrollDeliveryService(
        database, _settings(), transport=httpx.MockTransport(handler)
    )
    try:
        assert service.reconcile_due() == 0
        assert service.reconcile_due() == 0
    finally:
        service.close()
    payment = database.get_payroll_payment(payment_id)
    assert calls == 1
    assert payment["status"] == "processing"
    assert payment["reconcile_attempts"] == 1
    assert payment["last_reconcile_error"] == "wise_reconcile_http_503"
    assert "private" not in payment["last_reconcile_error"]
    assert datetime.fromisoformat(payment["next_reconcile_at"]) > datetime.now(UTC)


def test_provider_reference_and_reversed_state_are_immutable(database):
    owner, member, payment_id = _payroll(database)
    database.set_payroll_destination(
        member["id"], "wise", "8692237", owner["id"], "USD"
    )
    database.claim_payroll_delivery(payment_id, "wise")
    database.record_payroll_delivery(
        payment_id,
        "paid",
        provider="wise",
        external_reference="16521632",
    )
    with pytest.raises(ValueError, match="reference cannot be changed"):
        database.record_payroll_delivery(
            payment_id,
            "paid",
            provider="wise",
            external_reference="99999999",
        )
    database.record_payroll_delivery(
        payment_id,
        "reversed",
        provider="wise",
        external_reference="16521632",
        failure_reason="wise_charged_back",
    )
    with pytest.raises(ValueError, match="terminal"):
        database.record_payroll_delivery(
            payment_id,
            "paid",
            provider="wise",
            external_reference="16521632",
        )


def test_wise_webhook_is_signed_deduplicated_ordered_and_profile_scoped(database):
    owner, member, payment_id = _payroll(database)
    database.set_payroll_destination(
        member["id"], "wise", "8692237", owner["id"], "USD"
    )
    database.claim_payroll_delivery(payment_id, "wise")
    database.record_payroll_delivery(
        payment_id,
        "paid",
        provider="wise",
        external_reference="16521632",
    )
    private_key, public_key_b64 = _webhook_key_pair()
    service = PayrollDeliveryService(
        database, _settings(webhook_public_key_b64=public_key_b64)
    )
    event_at = datetime.now(UTC)
    body, signature = _signed_webhook(
        private_key, _state_event(16521632, "outgoing_payment_sent", event_at)
    )
    delivery_id = str(uuid.uuid4())
    try:
        with pytest.raises(PaymentDeliveryError, match="invalid_signature"):
            service.handle_wise_webhook(body, "AAAA", delivery_id)
        assert service.handle_wise_webhook(body, signature, delivery_id)["outcome"] == (
            "queued"
        )
        assert service.handle_wise_webhook(body, signature, delivery_id) == {
            "outcome": "duplicate"
        }

        stale_body, stale_signature = _signed_webhook(
            private_key,
            _state_event(16521632, "processing", event_at - timedelta(milliseconds=1)),
        )
        assert (
            service.handle_wise_webhook(stale_body, stale_signature, str(uuid.uuid4()))[
                "outcome"
            ]
            == "stale"
        )

        wrong_body, wrong_signature = _signed_webhook(
            private_key,
            _state_event(
                16521632,
                "funds_refunded",
                event_at + timedelta(milliseconds=1),
                profile_id=999,
            ),
        )
        assert service.handle_wise_webhook(
            wrong_body, wrong_signature, str(uuid.uuid4())
        ) == {"outcome": "wrong_profile"}

        unknown_body, unknown_signature = _signed_webhook(
            private_key,
            _state_event(99999999, "processing", event_at + timedelta(milliseconds=2)),
        )
        assert service.handle_wise_webhook(
            unknown_body, unknown_signature, str(uuid.uuid4())
        ) == {"outcome": "unknown_transfer"}

        test_body, test_signature = _signed_webhook(private_key, {})
        assert service.handle_wise_webhook(
            test_body,
            test_signature,
            str(uuid.uuid4()),
            test_notification=True,
        ) == {"outcome": "test"}
    finally:
        service.close()
    payment = database.get_payroll_payment(payment_id)
    assert payment["provider_status"] == "outgoing_payment_sent"
    assert datetime.fromisoformat(payment["provider_event_at"]) == event_at


def test_wise_webhook_revives_expired_follow_up_and_reconciles_refund(
    tmp_path, postgres_url
):
    private_key, public_key_b64 = _webhook_key_pair()
    provider_calls = 0

    def provider(request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        assert request.method == "GET"
        assert request.url.path.endswith("/transfers/16521632")
        return httpx.Response(200, json={"id": 16521632, "status": "funds_refunded"})

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
        payment_provider="wise",
        wise_api_token="wise-api-token-long-enough",
        wise_profile_id=101,
        wise_balance_id=202,
        wise_source_currency="GBP",
        wise_webhook_public_key_b64=public_key_b64,
    )
    app = create_app(settings, payment_transport=httpx.MockTransport(provider))
    with TestClient(app) as client:
        database = app.state.database
        owner, member, payment_id = _payroll(database)
        database.set_payroll_destination(
            member["id"], "wise", "8692237", owner["id"], "USD"
        )
        database.claim_payroll_delivery(payment_id, "wise")
        database.record_payroll_delivery(
            payment_id,
            "paid",
            provider="wise",
            external_reference="16521632",
        )
        with database.connect() as connection:
            connection.execute(
                """UPDATE payroll_payments
                   SET next_reconcile_at=%s,reconcile_until=%s WHERE id=%s""",
                (
                    datetime.now(UTC) - timedelta(days=2),
                    datetime.now(UTC) - timedelta(days=1),
                    payment_id,
                ),
            )

        payload = {
            "data": {
                "resource": {
                    "type": "transfer",
                    "id": 16521632,
                    "profile_id": 101,
                    "account_id": 202,
                    "refund_amount": 30,
                    "refund_currency": "USD",
                },
                "occurred_at": datetime.now(UTC).isoformat(),
            },
            "subscription_id": str(uuid.uuid4()),
            "event_type": "transfers#refund",
            "schema_version": "4.0.0",
            "sent_at": datetime.now(UTC).isoformat(),
        }
        body, signature = _signed_webhook(private_key, payload)
        delivery_id = str(uuid.uuid4())
        headers = {
            "Content-Type": "application/json",
            "X-Delivery-Id": delivery_id,
            "X-Signature-SHA256": signature,
        }
        rejected = client.post(
            "/api/v1/payroll/wise-webhook",
            content=body,
            headers={**headers, "X-Signature-SHA256": "AAAA"},
        )
        assert rejected.status_code == 401
        assert (
            client.post(
                "/api/v1/payroll/wise-webhook", content=body, headers=headers
            ).status_code
            == 202
        )
        assert (
            client.post(
                "/api/v1/payroll/wise-webhook", content=body, headers=headers
            ).status_code
            == 202
        )

        queued = database.get_payroll_payment(payment_id)
        assert queued["provider_status"] == "funds_refunded"
        assert queued["provider_failure_code"] == "funds_refunded"
        assert datetime.fromisoformat(queued["next_reconcile_at"]) <= datetime.now(UTC)
        assert datetime.fromisoformat(queued["reconcile_until"]) > datetime.now(UTC)
        _login(client, owner["email"], "owner password long enough")
        financials = client.get("/financials")
        assert "Provider alerts" in financials.text
        assert "Wise refund 30 USD" in financials.text
        assert app.state.payroll_delivery.reconcile_due() == 1
        assert database.get_payroll_payment(payment_id)["status"] == "reversed"
        with database.connect() as connection:
            delivery_count = connection.execute(
                """SELECT COUNT(*) count FROM integration_webhook_deliveries
                   WHERE provider='wise' AND delivery_id=%s""",
                (delivery_id,),
            ).fetchone()["count"]
        assert delivery_count == 1
    assert provider_calls == 1


def test_wise_destination_route_is_owner_confirmed_and_currency_bound(
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
        payment_provider="wise",
        wise_api_token="wise-api-token-long-enough",
        wise_profile_id=101,
        wise_balance_id=202,
        wise_source_currency="GBP",
    )

    def no_provider_call(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Destination management must not call Wise")

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
        assert "Wise recipient account ID" in people.text
        unconfirmed = client.post(
            f"/people/{member['id']}/payroll-destination",
            data={
                "provider": "wise",
                "recipient": "8692237",
                "currency": "USD",
                "csrf": _csrf(people),
            },
        )
        assert unconfirmed.status_code == 422
        bad_currency = client.post(
            f"/people/{member['id']}/payroll-destination",
            data={
                "provider": "wise",
                "recipient": "8692237",
                "currency": "US",
                "confirmed": "true",
                "csrf": _csrf(people),
            },
        )
        assert bad_currency.status_code == 422
        confirmed = client.post(
            f"/people/{member['id']}/payroll-destination",
            data={
                "provider": "wise",
                "recipient": "8692237",
                "currency": "usd",
                "confirmed": "true",
                "csrf": _csrf(people),
            },
            follow_redirects=False,
        )
        assert confirmed.status_code == 303
        destination = database.payroll_destinations("wise")[member["id"]]
        assert destination["recipient"] == "8692237"
        assert destination["currency"] == "USD"

        client.cookies.clear()
        _login(client, manager["email"], "manager password long enough")
        denied = client.post(
            f"/people/{member['id']}/payroll-destination",
            data={
                "provider": "wise",
                "recipient": "9999999",
                "currency": "USD",
                "confirmed": "true",
                "csrf": _csrf(client.get("/")),
            },
        )
        assert denied.status_code == 403

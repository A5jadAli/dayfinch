from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from api.config import Settings
from api.main import create_app
from api.security import hash_password


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


def _csrf(response) -> str:
    matched = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert matched, response.text
    return matched.group(1)


def _member(database, owner: dict, email: str) -> dict:
    _, token = database.create_invitation(email, owner["id"], 24)
    return database.accept_invitation(
        token, hash_password("member password long enough")
    )


def _login(client: TestClient, email: str, password: str) -> None:
    client.cookies.clear()
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf": _csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_member_invoice_submission_partial_payment_and_private_document(
    tmp_path, postgres_url
):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        owner = database.get_user_by_email("owner@example.test")
        member = _member(database, owner, "worker@example.test")
        outsider = _member(database, owner, "outsider@example.test")

        _login(client, member["email"], "member password long enough")
        financials = client.get("/financials")
        today = datetime.now(UTC).date()
        created = client.post(
            "/team-invoices/manual",
            data={
                "csrf": _csrf(financials),
                "issued_on": today.isoformat(),
                "due_on": (today + timedelta(days=14)).isoformat(),
                "description": "Contract development",
                "quantity": "2",
                "unit_price": "100",
                "currency": "USD",
                "purchase_order": "PO-42",
                "notes": "Milestone one",
            },
            follow_redirects=False,
        )
        assert created.status_code == 303
        invoice = database.list_team_invoices(member["id"])[0]
        assert invoice["total"] == Decimal("200.00")
        assert invoice["encrypted_document_key"].startswith("team-invoices/")
        document = client.get(f"/team-invoices/{invoice['id']}/document")
        assert document.status_code == 200
        updated = client.post(
            f"/team-invoices/{invoice['id']}",
            data={
                "csrf": _csrf(document),
                "issued_on": today.isoformat(),
                "due_on": (today + timedelta(days=21)).isoformat(),
                "description": "Updated contract development",
                "quantity": "2",
                "unit_price": "100",
                "purchase_order": "PO-42",
                "notes": "Updated milestone",
            },
            follow_redirects=False,
        )
        assert updated.status_code == 303
        assert (
            "Updated contract development"
            in client.get(updated.headers["location"]).text
        )

        _login(client, outsider["email"], "member password long enough")
        assert client.get(f"/team-invoices/{invoice['id']}/document").status_code == 404
        outsider_financials = client.get("/financials")
        assert invoice["number"] not in outsider_financials.text
        assert (
            client.post(
                f"/team-invoices/{invoice['id']}/submit",
                data={"csrf": _csrf(outsider_financials)},
            ).status_code
            == 404
        )

        _login(client, member["email"], "member password long enough")
        financials = client.get("/financials")
        submitted = client.post(
            f"/team-invoices/{invoice['id']}/submit",
            data={"csrf": _csrf(financials)},
            follow_redirects=False,
        )
        assert submitted.status_code == 303
        assert database.team_invoice_snapshot(invoice["id"])["status"] == "submitted"
        member_csv = client.get("/reports/team-invoices.csv")
        assert invoice["number"] in member_csv.text

        _login(client, owner["email"], "correct horse battery staple")
        owner_financials = client.get("/financials")
        sealed_before_payment = database.team_invoice_snapshot(invoice["id"])[
            "encrypted_document_sha256"
        ]
        real_storage = app.state.invoice_vault.storage

        class FailingStorage:
            def save_blob(self, *_args, **_kwargs):
                raise OSError("object storage unavailable")

        app.state.invoice_vault.storage = FailingStorage()
        partial = client.post(
            f"/team-invoices/{invoice['id']}/payments",
            data={
                "csrf": _csrf(owner_financials),
                "amount": "75",
                "paid_on": today.isoformat(),
                "reference": "BANK-1",
            },
            follow_redirects=False,
        )
        assert partial.status_code == 303
        snapshot = database.team_invoice_snapshot(invoice["id"])
        assert snapshot["status"] == "partially_paid"
        assert snapshot["paid_amount"] == Decimal("75.00")
        assert snapshot["amount_due"] == Decimal("125.00")
        assert snapshot["encrypted_document_sha256"] == sealed_before_payment
        app.state.invoice_vault.storage = real_storage
        # Opening a stale document heals the deferred encrypted snapshot.
        assert client.get(f"/team-invoices/{invoice['id']}/document").status_code == 200
        assert (
            database.team_invoice_snapshot(invoice["id"])["encrypted_document_sha256"]
            != sealed_before_payment
        )

        overpayment = client.post(
            f"/team-invoices/{invoice['id']}/payments",
            data={
                "csrf": _csrf(client.get("/financials")),
                "amount": "126",
                "paid_on": today.isoformat(),
            },
        )
        assert overpayment.status_code == 422
        assert database.team_invoice_snapshot(invoice["id"])["paid_amount"] == Decimal(
            "75.00"
        )

        paid = client.post(
            f"/team-invoices/{invoice['id']}/payments",
            data={
                "csrf": _csrf(client.get("/financials")),
                "amount": "125",
                "paid_on": today.isoformat(),
                "reference": "BANK-2",
            },
            follow_redirects=False,
        )
        assert paid.status_code == 303
        snapshot = database.team_invoice_snapshot(invoice["id"])
        assert snapshot["status"] == "paid"
        assert snapshot["amount_due"] == Decimal("0.00")
        document = client.get(f"/team-invoices/{invoice['id']}/document")
        assert document.status_code == 200
        assert "USD 200.00" in document.text


def test_tracked_time_invoice_prevents_double_billing_and_releases_deleted_draft(
    tmp_path, postgres_url
):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        owner = database.get_user_by_email("owner@example.test")
        member = _member(database, owner, "worker@example.test")
        database.set_user_profile(
            member["id"], "member", "Worker", Decimal("60"), Decimal("100"), 0, 0
        )
        project = database.create_project("Invoice project", "", owner["id"])
        database.add_project_member(project["id"], member["id"], "worker")
        device, _ = database.create_device("Laptop", member["id"], project["id"])
        now = datetime.now(UTC)
        database.sync_work_session(
            device,
            "active",
            None,
            project["id"],
            observed_at=now - timedelta(hours=2),
        )
        database.sync_work_session(
            device,
            "stopped",
            None,
            project["id"],
            observed_at=now - timedelta(hours=1),
        )

        _login(client, member["email"], "member password long enough")
        financials = client.get("/financials")
        today = now.date()
        payload = {
            "csrf": _csrf(financials),
            "issued_on": today.isoformat(),
            "due_on": (today + timedelta(days=7)).isoformat(),
            "period_start": (now - timedelta(hours=2)).date().isoformat(),
            "period_end": (now - timedelta(hours=1)).date().isoformat(),
            "project_id": project["id"],
            "currency": "USD",
        }
        created = client.post(
            "/team-invoices/tracked", data=payload, follow_redirects=False
        )
        assert created.status_code == 303
        first = database.list_team_invoices(member["id"])[0]
        assert first["total"] == Decimal("60.00")

        duplicate = client.post("/team-invoices/tracked", data=payload)
        assert duplicate.status_code == 422
        assert "No uninvoiced completed time" in duplicate.text

        deleted = client.post(
            f"/team-invoices/{first['id']}/delete",
            data={"csrf": _csrf(client.get("/financials"))},
            follow_redirects=False,
        )
        assert deleted.status_code == 303
        recreated = client.post(
            "/team-invoices/tracked",
            data={**payload, "csrf": _csrf(client.get("/financials"))},
            follow_redirects=False,
        )
        assert recreated.status_code == 303


def test_financial_team_lead_can_manage_only_assigned_member_invoices(
    tmp_path, postgres_url
):
    app = create_app(_settings(tmp_path, postgres_url))
    with TestClient(app) as client:
        database = app.state.database
        owner = database.get_user_by_email("owner@example.test")
        lead = _member(database, owner, "lead@example.test")
        worker = _member(database, owner, "worker@example.test")
        outsider = _member(database, owner, "outsider@example.test")
        team_id = database.create_team("Billing team", lead["id"])
        database.set_team_lead_permissions(
            team_id, lead["id"], {"manage_financials": True}
        )
        database.add_team_member(team_id, worker["id"])
        today = datetime.now(UTC).date()

        def submitted_invoice(member: dict, description: str) -> str:
            invoice_id = database.create_team_invoice(
                member["id"],
                today,
                today + timedelta(days=7),
                description,
                Decimal("1"),
                Decimal("50"),
                "USD",
            )
            app.state.invoice_vault.seal_team(invoice_id)
            database.submit_team_invoice(invoice_id, member["id"])
            app.state.invoice_vault.seal_team(invoice_id)
            return invoice_id

        worker_invoice = submitted_invoice(worker, "Worker invoice")
        outsider_invoice = submitted_invoice(outsider, "Outsider invoice")
        void_invoice = submitted_invoice(worker, "Void this invoice")

        _login(client, lead["email"], "member password long enough")
        financials = client.get("/financials")
        assert (
            "Worker invoice" not in financials.text
        )  # list shows invoice metadata only
        assert (
            database.team_invoice_snapshot(worker_invoice)["number"] in financials.text
        )
        assert (
            database.team_invoice_snapshot(outsider_invoice)["number"]
            not in financials.text
        )
        assert (
            client.get(f"/team-invoices/{worker_invoice}/document").status_code == 200
        )
        assert (
            client.get(f"/team-invoices/{outsider_invoice}/document").status_code == 404
        )
        exported = client.get("/reports/team-invoices.csv")
        assert database.team_invoice_snapshot(worker_invoice)["number"] in exported.text
        assert (
            database.team_invoice_snapshot(outsider_invoice)["number"]
            not in exported.text
        )

        voided = client.post(
            f"/team-invoices/{void_invoice}/void",
            data={"csrf": _csrf(client.get(f"/team-invoices/{void_invoice}/document"))},
            follow_redirects=False,
        )
        assert voided.status_code == 303
        assert database.team_invoice_snapshot(void_invoice)["status"] == "void"
        denied = client.post(
            f"/team-invoices/{outsider_invoice}/payments",
            data={
                "csrf": _csrf(client.get("/financials")),
                "amount": "10",
                "paid_on": today.isoformat(),
            },
        )
        assert denied.status_code == 404

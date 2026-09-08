import base64
import re
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from api.config import Settings
from api.main import create_app
from api.security import hash_password
from api.services.invoice_vault import MAGIC, InvoiceVault, InvoiceVaultError
from api.storage import LocalScreenshotStorage


def _settings(tmp_path, key: bytes = b"k" * 32):
    return Settings(
        data_dir=tmp_path,
        admin_password="safe test password",
        session_secret="safe session secret" * 3,
        cookie_secure=False,
        max_upload_bytes=1024,
        retention_days=30,
        document_encryption_key=base64.urlsafe_b64encode(key).decode().rstrip("="),
    )


def _invoice(database):
    admin = database.bootstrap_admin(
        "vault@example.test", hash_password("long test password")
    )
    client = database.create_client(
        "Private Client", "finance@private.test", "10 Confidential Road"
    )
    invoice_id = database.create_invoice(
        client,
        date(2026, 9, 1),
        date(2026, 9, 30),
        "Secret consulting engagement",
        Decimal("2.5"),
        Decimal("150"),
        "USD",
        admin["id"],
    )
    return invoice_id


def test_invoice_snapshot_is_aes_gcm_encrypted_and_round_trips(database, tmp_path):
    invoice_id = _invoice(database)
    storage = LocalScreenshotStorage(tmp_path / "private")
    vault = InvoiceVault(database, storage, _settings(tmp_path))

    sealed = vault.seal(invoice_id)
    raw = storage.read_blob(sealed["encrypted_document_key"])
    assert raw.startswith(MAGIC)
    assert b"Private Client" not in raw
    assert b"Secret consulting" not in raw
    opened = vault.open(invoice_id)
    assert opened["client_email"] == "finance@private.test"
    assert opened["lines"][0]["description"] == "Secret consulting engagement"
    assert Decimal(opened["subtotal"]) == Decimal("375.00")


def test_invoice_tampering_or_wrong_key_is_rejected(database, tmp_path):
    invoice_id = _invoice(database)
    storage = LocalScreenshotStorage(tmp_path / "private")
    vault = InvoiceVault(database, storage, _settings(tmp_path))
    sealed = vault.seal(invoice_id)

    with pytest.raises(InvoiceVaultError, match="authentication"):
        InvoiceVault(database, storage, _settings(tmp_path, b"x" * 32)).open(invoice_id)

    path = storage.resolve(sealed["encrypted_document_key"])
    tampered = bytearray(path.read_bytes())
    tampered[-1] ^= 1
    path.write_bytes(tampered)
    with pytest.raises(InvoiceVaultError, match="integrity"):
        vault.open(invoice_id)


def test_invoice_creation_seals_and_authorized_route_renders_document(
    postgres_url, tmp_path
):
    settings = _settings(tmp_path)
    settings = replace(
        settings,
        admin_email="invoice-admin@example.test",
        database_url=postgres_url,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        login_page = client.get("/login")
        csrf = re.search(r'name="csrf" value="([^"]+)"', login_page.text).group(1)
        login = client.post(
            "/login",
            data={
                "email": settings.admin_email,
                "password": settings.admin_password,
                "csrf": csrf,
            },
            follow_redirects=False,
        )
        assert login.status_code == 303
        database = app.state.database
        client_id = database.create_client("Route Client", "billing@route.test")
        financials = client.get("/financials")
        csrf = re.search(r'name="csrf" value="([^"]+)"', financials.text).group(1)

        created = client.post(
            "/invoices",
            data={
                "client_id": client_id,
                "issued_on": "2026-09-01",
                "due_on": "2026-09-30",
                "description": "Encrypted route work",
                "quantity": "3",
                "unit_price": "50",
                "currency": "USD",
                "csrf": csrf,
            },
            follow_redirects=False,
        )
        assert created.status_code == 303
        invoice = database.finance_summary()["invoices"][0]
        assert invoice["encrypted_document_key"].endswith(".dfenc")

        document = client.get(f"/invoices/{invoice['id']}/document")
        assert document.status_code == 200
        assert "Encrypted route work" in document.text
        assert document.headers["cache-control"] == "private, no-store"

        changed = client.post(
            f"/financials/invoice/{invoice['id']}/status",
            data={"item_status": "sent", "csrf": csrf},
            follow_redirects=False,
        )
        assert changed.status_code == 303
        resealed = database.invoice_snapshot(invoice["id"])
        assert (
            resealed["encrypted_document_sha256"]
            != invoice["encrypted_document_sha256"]
        )
        document = client.get(f"/invoices/{invoice['id']}/document")
        assert "sent" in document.text.lower()

        invalid = client.post(
            f"/financials/invoice/{invoice['id']}/status",
            data={"item_status": "unknown", "csrf": csrf},
        )
        assert invalid.status_code == 422

        missing = client.post(
            "/financials/invoice/00000000-0000-0000-0000-000000000000/status",
            data={"item_status": "paid", "csrf": csrf},
        )
        assert missing.status_code == 422

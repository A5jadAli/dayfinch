from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..config import Settings
from ..database import Database
from ..storage import ScreenshotStore

MAGIC = b"DFINV1"


class InvoiceVaultError(RuntimeError):
    pass


def _key(settings: Settings) -> bytes:
    configured = settings.document_encryption_key
    if configured:
        try:
            value = base64.urlsafe_b64decode(configured + "=" * (-len(configured) % 4))
        except ValueError as exc:
            raise InvoiceVaultError(
                "Document encryption key is not valid base64"
            ) from exc
        if len(value) != 32:
            raise InvoiceVaultError("Document encryption key must decode to 32 bytes")
        return value
    return hashlib.sha256(
        b"dayfinch:invoice:v1\0" + settings.session_secret.encode()
    ).digest()


class InvoiceVault:
    """AES-256-GCM sealed invoice snapshots stored locally or in private S3."""

    def __init__(
        self, database: Database, storage: ScreenshotStore, settings: Settings
    ):
        self.database = database
        self.storage = storage
        self.aead = AESGCM(_key(settings))

    def seal(self, invoice_id: str) -> dict[str, Any]:
        invoice = self.database.invoice_snapshot(invoice_id)
        if not invoice:
            raise InvoiceVaultError("Invoice not found")
        self._seal_snapshot(
            invoice_id,
            invoice,
            f"invoices/{invoice_id}.dfenc",
            invoice_id.encode(),
            self.database.record_invoice_document,
        )
        return self.database.invoice_snapshot(invoice_id) or {}

    def seal_team(self, invoice_id: str) -> dict[str, Any]:
        invoice = self.database.team_invoice_snapshot(invoice_id)
        if not invoice:
            raise InvoiceVaultError("Team invoice not found")
        self._seal_snapshot(
            invoice_id,
            invoice,
            f"team-invoices/{invoice_id}.dfenc",
            f"team:{invoice_id}".encode(),
            self.database.record_team_invoice_document,
        )
        return self.database.team_invoice_snapshot(invoice_id) or {}

    def _seal_snapshot(
        self,
        invoice_id: str,
        invoice: dict[str, Any],
        key: str,
        associated_data: bytes,
        recorder,
    ) -> None:
        plaintext = json.dumps(
            invoice, default=str, sort_keys=True, separators=(",", ":")
        ).encode()
        nonce = os.urandom(12)
        ciphertext = (
            MAGIC + nonce + self.aead.encrypt(nonce, plaintext, associated_data)
        )
        try:
            stored = self.storage.save_blob(key, ciphertext, "application/octet-stream")
        except Exception as exc:
            raise InvoiceVaultError("Encrypted invoice storage failed") from exc
        digest = hashlib.sha256(ciphertext).hexdigest()
        recorder(invoice_id, stored.key, stored.version_id, digest)

    def open(self, invoice_id: str) -> dict[str, Any]:
        invoice = self.database.invoice_snapshot(invoice_id)
        if not invoice or not invoice.get("encrypted_document_key"):
            raise InvoiceVaultError("Invoice document has not been sealed")
        return self._open_snapshot(invoice, invoice_id.encode())

    def open_team(self, invoice_id: str) -> dict[str, Any]:
        invoice = self.database.team_invoice_snapshot(invoice_id)
        if not invoice or not invoice.get("encrypted_document_key"):
            raise InvoiceVaultError("Team invoice document has not been sealed")
        return self._open_snapshot(invoice, f"team:{invoice_id}".encode())

    def _open_snapshot(
        self, invoice: dict[str, Any], associated_data: bytes
    ) -> dict[str, Any]:
        try:
            ciphertext = self.storage.read_blob(
                invoice["encrypted_document_key"], invoice.get("document_version_id")
            )
        except Exception as exc:
            raise InvoiceVaultError(
                "Encrypted invoice document is unavailable"
            ) from exc
        if (
            not hashlib.sha256(ciphertext).hexdigest()
            == invoice["encrypted_document_sha256"]
        ):
            raise InvoiceVaultError("Encrypted invoice integrity check failed")
        if not ciphertext.startswith(MAGIC) or len(ciphertext) < len(MAGIC) + 13:
            raise InvoiceVaultError("Encrypted invoice format is invalid")
        nonce = ciphertext[len(MAGIC) : len(MAGIC) + 12]
        try:
            plaintext = self.aead.decrypt(
                nonce, ciphertext[len(MAGIC) + 12 :], associated_data
            )
            return json.loads(plaintext)
        except (InvalidTag, ValueError, json.JSONDecodeError) as exc:
            raise InvoiceVaultError("Encrypted invoice authentication failed") from exc

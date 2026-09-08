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
        plaintext = json.dumps(
            invoice, default=str, sort_keys=True, separators=(",", ":")
        ).encode()
        nonce = os.urandom(12)
        ciphertext = (
            MAGIC + nonce + self.aead.encrypt(nonce, plaintext, invoice_id.encode())
        )
        key = f"invoices/{invoice_id}.dfenc"
        stored = self.storage.save_blob(key, ciphertext, "application/octet-stream")
        digest = hashlib.sha256(ciphertext).hexdigest()
        self.database.record_invoice_document(
            invoice_id, stored.key, stored.version_id, digest
        )
        return self.database.invoice_snapshot(invoice_id) or {}

    def open(self, invoice_id: str) -> dict[str, Any]:
        invoice = self.database.invoice_snapshot(invoice_id)
        if not invoice or not invoice.get("encrypted_document_key"):
            raise InvoiceVaultError("Invoice document has not been sealed")
        ciphertext = self.storage.read_blob(invoice["encrypted_document_key"])
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
                nonce, ciphertext[len(MAGIC) + 12 :], invoice_id.encode()
            )
            return json.loads(plaintext)
        except (InvalidTag, ValueError, json.JSONDecodeError) as exc:
            raise InvoiceVaultError("Encrypted invoice authentication failed") from exc

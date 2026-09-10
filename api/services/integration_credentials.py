from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..database import Database

MAGIC = b"DFCRED1"
MAX_KEY_SPEC_BYTES = 4096
MAX_KEYS = 8
MAX_PLAINTEXT_BYTES = 64 * 1024
MAX_CIPHERTEXT_BYTES = 128 * 1024
_KEY_ID = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_PROVIDER = re.compile(r"^[a-z][a-z0-9_-]{0,59}$")
LOGGER = logging.getLogger("dayfinch-integration-credentials")


class IntegrationCredentialError(RuntimeError):
    """A privacy-safe provider credential storage failure."""


@dataclass(frozen=True)
class CredentialKeyring:
    primary_id: str
    keys: Mapping[str, bytes]

    @classmethod
    def parse(cls, specification: str) -> CredentialKeyring:
        if not specification or len(specification.encode()) > MAX_KEY_SPEC_BYTES:
            raise ValueError("Integration credential keyring is missing or too large")
        parsed: dict[str, bytes] = {}
        for entry in specification.split(","):
            key_id, separator, encoded = entry.strip().partition(":")
            if (
                not separator
                or not _KEY_ID.fullmatch(key_id)
                or not encoded
                or key_id in parsed
            ):
                raise ValueError("Integration credential keyring entry is invalid")
            try:
                key = base64.b64decode(
                    encoded + "=" * (-len(encoded) % 4),
                    altchars=b"-_",
                    validate=True,
                )
            except (ValueError, binascii.Error) as exc:
                raise ValueError(
                    "Integration credential key is not valid base64url"
                ) from exc
            if len(key) != 32:
                raise ValueError("Integration credential keys must contain 32 bytes")
            if key in parsed.values():
                raise ValueError("Integration credential key material is duplicated")
            parsed[key_id] = key
            if len(parsed) > MAX_KEYS:
                raise ValueError("Too many integration credential keys")
        if not parsed:
            raise ValueError("Integration credential keyring is empty")
        return cls(next(iter(parsed)), MappingProxyType(parsed))


@dataclass(frozen=True)
class OpenedCredential:
    integration_id: str
    provider: str
    revision: int
    access_expires_at: datetime | str | None
    values: Mapping[str, Any]
    subject_user_id: str | None = None


@dataclass(frozen=True)
class ClaimedCredential:
    credential: OpenedCredential
    claim_token: str


class IntegrationCredentialVault:
    """AES-256-GCM provider secrets with online key rotation and refresh leases."""

    def __init__(self, database: Database, keyring: CredentialKeyring):
        self.database = database
        self.keyring = keyring

    @staticmethod
    def _identity(
        integration_id: str, provider: str, subject_user_id: str | None = None
    ) -> tuple[str, str, str | None]:
        try:
            normalized_id = str(uuid.UUID(integration_id))
        except (ValueError, AttributeError) as exc:
            raise IntegrationCredentialError(
                "Integration credential identity is invalid"
            ) from exc
        if not _PROVIDER.fullmatch(provider):
            raise IntegrationCredentialError(
                "Integration credential provider is invalid"
            )
        normalized_user_id: str | None = None
        if subject_user_id is not None:
            try:
                normalized_user_id = str(uuid.UUID(subject_user_id))
            except (ValueError, AttributeError) as exc:
                raise IntegrationCredentialError(
                    "Integration credential subject is invalid"
                ) from exc
        return normalized_id, provider, normalized_user_id

    @staticmethod
    def _associated_data(
        integration_id: str,
        provider: str,
        key_id: str,
        subject_user_id: str | None = None,
    ) -> bytes:
        subject = f":user:{subject_user_id}" if subject_user_id else ""
        return f"dayfinch:integration-credential:v1:{provider}:{integration_id}:{key_id}{subject}".encode()

    @staticmethod
    def _payload(values: Mapping[str, Any]) -> bytes:
        if not isinstance(values, Mapping) or not values:
            raise IntegrationCredentialError(
                "Integration credential payload is invalid"
            )
        try:
            payload = json.dumps(
                dict(values), sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        except (TypeError, ValueError) as exc:
            raise IntegrationCredentialError(
                "Integration credential payload is invalid"
            ) from exc
        if not payload or len(payload) > MAX_PLAINTEXT_BYTES:
            raise IntegrationCredentialError(
                "Integration credential payload is too large"
            )
        return payload

    def _encrypt(
        self,
        integration_id: str,
        provider: str,
        values: Mapping[str, Any],
        subject_user_id: str | None = None,
    ) -> tuple[str, bytes]:
        integration_id, provider, subject_user_id = self._identity(
            integration_id, provider, subject_user_id
        )
        key_id = self.keyring.primary_id
        nonce = os.urandom(12)
        ciphertext = (
            MAGIC
            + nonce
            + AESGCM(self.keyring.keys[key_id]).encrypt(
                nonce,
                self._payload(values),
                self._associated_data(
                    integration_id, provider, key_id, subject_user_id
                ),
            )
        )
        if len(ciphertext) > MAX_CIPHERTEXT_BYTES:
            raise IntegrationCredentialError(
                "Integration credential payload is too large"
            )
        return key_id, ciphertext

    def store(
        self,
        integration_id: str,
        provider: str,
        values: Mapping[str, Any],
        *,
        access_expires_at: datetime | None = None,
        subject_user_id: str | None = None,
    ) -> int:
        key_id, ciphertext = self._encrypt(
            integration_id, provider, values, subject_user_id
        )
        try:
            if subject_user_id is not None:
                return self.database.store_user_integration_credentials(
                    integration_id,
                    subject_user_id,
                    provider,
                    key_id,
                    ciphertext,
                    access_expires_at,
                )
            return self.database.store_integration_credentials(
                integration_id,
                provider,
                key_id,
                ciphertext,
                access_expires_at,
            )
        except Exception as exc:
            raise IntegrationCredentialError(
                "Integration credential could not be stored"
            ) from exc

    def _decrypt_row(self, row: Mapping[str, Any]) -> Mapping[str, Any]:
        key_id = str(row["key_id"])
        key = self.keyring.keys.get(key_id)
        ciphertext = bytes(row["ciphertext"])
        if key is None:
            raise IntegrationCredentialError(
                "Integration credential encryption key is unavailable"
            )
        if (
            not ciphertext.startswith(MAGIC)
            or len(ciphertext) < len(MAGIC) + 12 + 16
            or len(ciphertext) > MAX_CIPHERTEXT_BYTES
        ):
            raise IntegrationCredentialError("Integration credential format is invalid")
        nonce_start = len(MAGIC)
        nonce = ciphertext[nonce_start : nonce_start + 12]
        subject_user_id = (
            str(row["user_id"]) if row.get("user_id") is not None else None
        )
        try:
            plaintext = AESGCM(key).decrypt(
                nonce,
                ciphertext[nonce_start + 12 :],
                self._associated_data(
                    str(row["integration_id"]),
                    str(row["provider"]),
                    key_id,
                    subject_user_id,
                ),
            )
            values = json.loads(plaintext)
        except (
            InvalidTag,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            raise IntegrationCredentialError(
                "Integration credential authentication failed"
            ) from exc
        if not isinstance(values, dict) or not values:
            raise IntegrationCredentialError(
                "Integration credential payload is invalid"
            )
        return MappingProxyType(values)

    def open(
        self,
        integration_id: str,
        provider: str,
        *,
        subject_user_id: str | None = None,
    ) -> OpenedCredential:
        integration_id, provider, subject_user_id = self._identity(
            integration_id, provider, subject_user_id
        )
        for _attempt in range(3):
            try:
                if subject_user_id is not None:
                    row = self.database.get_user_integration_credentials(
                        integration_id, subject_user_id, provider
                    )
                else:
                    row = self.database.get_integration_credentials(
                        integration_id, provider
                    )
            except Exception as exc:
                raise IntegrationCredentialError(
                    "Integration credential could not be read"
                ) from exc
            if not row:
                raise IntegrationCredentialError(
                    "Integration credential is unavailable"
                )
            values = self._decrypt_row(row)
            revision = int(row["revision"])
            if row["key_id"] == self.keyring.primary_id:
                return OpenedCredential(
                    integration_id=integration_id,
                    provider=provider,
                    revision=revision,
                    access_expires_at=row["access_expires_at"],
                    values=values,
                    subject_user_id=subject_user_id,
                )
            key_id, ciphertext = self._encrypt(
                integration_id, provider, values, subject_user_id
            )
            try:
                if subject_user_id is not None:
                    rotated = self.database.rotate_user_integration_credentials(
                        integration_id,
                        subject_user_id,
                        provider,
                        revision,
                        key_id,
                        ciphertext,
                    )
                else:
                    rotated = self.database.rotate_integration_credentials(
                        integration_id,
                        provider,
                        revision,
                        key_id,
                        ciphertext,
                    )
            except Exception as exc:
                raise IntegrationCredentialError(
                    "Integration credential could not be rotated"
                ) from exc
            if rotated:
                return OpenedCredential(
                    integration_id=integration_id,
                    provider=provider,
                    revision=revision + 1,
                    access_expires_at=row["access_expires_at"],
                    values=values,
                    subject_user_id=subject_user_id,
                )
            # A refresh or another rewrapper won the revision race. Refetch it;
            # returning the old decrypted refresh token could invalidate the grant.
        raise IntegrationCredentialError("Integration credential changed repeatedly")

    def store_pending(
        self,
        pending_id: str,
        user_id: str,
        provider: str,
        values: Mapping[str, Any],
        *,
        expires_at: datetime,
    ) -> None:
        pending_id, provider, user_id = self._identity(pending_id, provider, user_id)
        assert user_id is not None
        key_id, ciphertext = self._encrypt(pending_id, provider, values, user_id)
        try:
            self.database.store_pending_oauth_credentials(
                pending_id,
                user_id,
                provider,
                key_id,
                ciphertext,
                expires_at,
            )
        except Exception as exc:
            raise IntegrationCredentialError(
                "Pending integration authorization could not be stored"
            ) from exc

    def open_pending(
        self, pending_id: str, user_id: str, provider: str
    ) -> OpenedCredential:
        pending_id, provider, user_id = self._identity(pending_id, provider, user_id)
        assert user_id is not None
        try:
            row = self.database.get_pending_oauth_credentials(
                pending_id, user_id, provider
            )
        except Exception as exc:
            raise IntegrationCredentialError(
                "Pending integration authorization could not be read"
            ) from exc
        if not row:
            raise IntegrationCredentialError(
                "Pending integration authorization is unavailable"
            )
        values = self._decrypt_row(row)
        return OpenedCredential(
            integration_id=pending_id,
            provider=provider,
            revision=1,
            access_expires_at=row["access_expires_at"],
            values=values,
            subject_user_id=user_id,
        )

    def delete_pending(self, pending_id: str, user_id: str, provider: str) -> bool:
        pending_id, provider, user_id = self._identity(pending_id, provider, user_id)
        assert user_id is not None
        try:
            return self.database.delete_pending_oauth_credentials(
                pending_id, user_id, provider
            )
        except Exception as exc:
            raise IntegrationCredentialError(
                "Pending integration authorization could not be deleted"
            ) from exc

    def claim_for_refresh(
        self,
        integration_id: str,
        provider: str,
        observed_at: datetime,
        *,
        lease_seconds: int = 90,
        subject_user_id: str | None = None,
    ) -> ClaimedCredential | None:
        integration_id, provider, subject_user_id = self._identity(
            integration_id, provider, subject_user_id
        )
        claim_token = str(uuid.uuid4())
        try:
            if subject_user_id is not None:
                row = self.database.claim_user_integration_credentials(
                    integration_id,
                    subject_user_id,
                    provider,
                    claim_token,
                    observed_at,
                    lease_seconds=lease_seconds,
                )
            else:
                row = self.database.claim_integration_credentials(
                    integration_id,
                    provider,
                    claim_token,
                    observed_at,
                    lease_seconds=lease_seconds,
                )
        except Exception as exc:
            raise IntegrationCredentialError(
                "Integration credential refresh could not be claimed"
            ) from exc
        if not row:
            return None
        try:
            values = self._decrypt_row(row)
        except IntegrationCredentialError:
            try:
                if subject_user_id is not None:
                    self.database.release_user_integration_credential_claim(
                        integration_id, subject_user_id, provider, claim_token
                    )
                else:
                    self.database.release_integration_credential_claim(
                        integration_id, provider, claim_token
                    )
            except Exception:
                LOGGER.exception("integration_credential_claim_release_failed")
            raise
        return ClaimedCredential(
            credential=OpenedCredential(
                integration_id=integration_id,
                provider=provider,
                revision=int(row["revision"]),
                access_expires_at=row["access_expires_at"],
                values=values,
                subject_user_id=subject_user_id,
            ),
            claim_token=claim_token,
        )

    def release_refresh(self, claim: ClaimedCredential) -> bool:
        try:
            if claim.credential.subject_user_id is not None:
                return self.database.release_user_integration_credential_claim(
                    claim.credential.integration_id,
                    claim.credential.subject_user_id,
                    claim.credential.provider,
                    claim.claim_token,
                )
            return self.database.release_integration_credential_claim(
                claim.credential.integration_id,
                claim.credential.provider,
                claim.claim_token,
            )
        except Exception as exc:
            raise IntegrationCredentialError(
                "Integration credential refresh could not be released"
            ) from exc

    def rewrap_all(self, *, limit: int = 100) -> dict[str, int]:
        try:
            rows = self.database.integration_credentials_requiring_rewrap(
                self.keyring.primary_id, limit=limit
            )
        except Exception as exc:
            raise IntegrationCredentialError(
                "Integration credentials could not be listed for rotation"
            ) from exc
        rewrapped = 0
        failed = 0
        for row in rows:
            try:
                opened = self.open(
                    str(row["integration_id"]),
                    str(row["provider"]),
                    subject_user_id=(
                        str(row["user_id"]) if row.get("user_id") is not None else None
                    ),
                )
            except IntegrationCredentialError:
                failed += 1
                LOGGER.error("integration_credential_rewrap_failed")
            else:
                if opened.revision > 1:
                    rewrapped += 1
        return {"examined": len(rows), "rewrapped": rewrapped, "failed": failed}

    def replace_after_refresh(
        self,
        integration_id: str,
        provider: str,
        claim_token: str,
        expected_revision: int,
        values: Mapping[str, Any],
        *,
        access_expires_at: datetime | None = None,
        subject_user_id: str | None = None,
    ) -> int:
        key_id, ciphertext = self._encrypt(
            integration_id, provider, values, subject_user_id
        )
        try:
            if subject_user_id is not None:
                return self.database.replace_claimed_user_integration_credentials(
                    integration_id,
                    subject_user_id,
                    provider,
                    claim_token,
                    expected_revision,
                    key_id,
                    ciphertext,
                    access_expires_at,
                )
            return self.database.replace_claimed_integration_credentials(
                integration_id,
                provider,
                claim_token,
                expected_revision,
                key_id,
                ciphertext,
                access_expires_at,
            )
        except Exception as exc:
            raise IntegrationCredentialError(
                "Integration credential refresh ownership was lost"
            ) from exc

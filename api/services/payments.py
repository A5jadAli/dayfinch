from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import UUID

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asymmetric_padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from ..config import Settings
from ..database import Database

LOGGER = logging.getLogger("dayfinch-payroll")
MAX_PROVIDER_RESPONSE_BYTES = 1024 * 1024
MAX_WISE_WEBHOOK_BYTES = 1024 * 1024
PAYPAL_REFERENCE = re.compile(r"^[A-Za-z0-9_-]{1,30}$")
PAYPAL_CURRENCIES = frozenset(
    {
        "AUD",
        "BRL",
        "CAD",
        "CHF",
        "CZK",
        "DKK",
        "EUR",
        "GBP",
        "HKD",
        "HUF",
        "ILS",
        "JPY",
        "MXN",
        "MYR",
        "NOK",
        "NZD",
        "PHP",
        "PLN",
        "SEK",
        "SGD",
        "THB",
        "TWD",
        "USD",
    }
)
PAYPAL_ZERO_DECIMAL_CURRENCIES = frozenset({"HUF", "JPY", "TWD"})
WISE_TRANSFER_REFERENCE = re.compile(r"^[1-9][0-9]{0,18}$")


class PaymentDeliveryError(RuntimeError):
    pass


class PayrollDeliveryService:
    """Idempotent payroll dispatch and provider-side status reconciliation."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        self.database = database
        self.settings = settings
        self._client = httpx.Client(
            base_url=settings.paypal_api_url,
            transport=transport,
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=False,
            headers={
                "Accept": "application/json",
                "User-Agent": "Dayfinch-Payroll/1.0",
            },
        )
        self._wise_client = httpx.Client(
            base_url=settings.wise_api_url + "/",
            transport=transport,
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=False,
            headers={
                "Accept": "application/json",
                "User-Agent": "Dayfinch-Payroll/1.0",
            },
        )
        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()
        public_key = serialization.load_pem_public_key(
            settings.wise_webhook_public_key_pem
        )
        if not isinstance(public_key, RSAPublicKey):
            raise ValueError("Wise webhook key must be RSA")
        self._wise_webhook_public_key = public_key

    @property
    def provider(self) -> str:
        return self.settings.payment_provider

    @property
    def configured(self) -> bool:
        if self.provider == "paypal":
            return bool(
                self.settings.paypal_client_id and self.settings.paypal_client_secret
            )
        if self.provider == "wise":
            return bool(
                self.settings.wise_api_token
                and self.settings.wise_profile_id > 0
                and self.settings.wise_balance_id > 0
            )
        if self.provider == "webhook":
            return bool(
                self.settings.payment_webhook_url
                and self.settings.payment_webhook_secret
            )
        return False

    @property
    def wise_webhook_enabled(self) -> bool:
        return bool(self.settings.wise_api_token and self.settings.wise_profile_id > 0)

    def close(self) -> None:
        self._client.close()
        self._wise_client.close()

    def verify_wise_webhook(self, body: bytes, signature: str) -> bool:
        if not self.wise_webhook_enabled or len(body) > MAX_WISE_WEBHOOK_BYTES:
            return False
        try:
            decoded_signature = base64.b64decode(signature, validate=True)
            self._wise_webhook_public_key.verify(
                decoded_signature,
                body,
                asymmetric_padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except (binascii.Error, InvalidSignature, TypeError, ValueError):
            return False
        return True

    @staticmethod
    def _wise_event_time(value: object) -> datetime:
        if not isinstance(value, str) or not value or len(value) > 40:
            raise PaymentDeliveryError("wise_webhook_invalid_event_time")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PaymentDeliveryError("wise_webhook_invalid_event_time") from exc
        if parsed.tzinfo is None:
            raise PaymentDeliveryError("wise_webhook_invalid_event_time")
        return parsed.astimezone(UTC)

    @staticmethod
    def _wise_event_positive_id(value: object, label: str) -> str:
        if isinstance(value, bool):
            raise PaymentDeliveryError(f"wise_webhook_invalid_{label}")
        candidate = str(value or "")
        if not WISE_TRANSFER_REFERENCE.fullmatch(candidate):
            raise PaymentDeliveryError(f"wise_webhook_invalid_{label}")
        return candidate

    def handle_wise_webhook(
        self,
        body: bytes,
        signature: str,
        delivery_id: str,
        *,
        test_notification: bool = False,
    ) -> dict:
        """Authenticate, deduplicate, and durably queue a canonical Wise refresh."""

        if not self.wise_webhook_enabled:
            raise PaymentDeliveryError("wise_webhook_not_configured")
        if not self.verify_wise_webhook(body, signature):
            raise PaymentDeliveryError("wise_webhook_invalid_signature")
        try:
            if str(UUID(delivery_id)) != delivery_id.lower():
                raise ValueError
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise PaymentDeliveryError("wise_webhook_invalid_payload") from exc
        if not isinstance(payload, dict):
            raise PaymentDeliveryError("wise_webhook_invalid_payload")
        if test_notification:
            if not self.database.begin_integration_webhook(
                "wise", delivery_id, "test-notification", datetime.now(UTC)
            ):
                return {"outcome": "duplicate"}
            self.database.finish_integration_webhook("wise", delivery_id, "ignored")
            return {"outcome": "test"}

        event_type = payload.get("event_type")
        if event_type not in {
            "transfers#state-change",
            "transfers#payout-failure",
            "transfers#refund",
        }:
            raise PaymentDeliveryError("wise_webhook_unsupported_event")
        if payload.get("schema_version") != "4.0.0":
            raise PaymentDeliveryError("wise_webhook_unsupported_schema")
        try:
            UUID(str(payload["subscription_id"]))
            data = payload["data"]
            if not isinstance(data, dict):
                raise TypeError
            occurred_at = self._wise_event_time(data["occurred_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PaymentDeliveryError("wise_webhook_invalid_payload") from exc

        failure_code = ""
        failure_description = ""
        if event_type == "transfers#payout-failure":
            transfer_id = self._wise_event_positive_id(
                data.get("transfer_id"), "transfer_id"
            )
            profile_id = self._wise_event_positive_id(
                data.get("profile_id"), "profile_id"
            )
            provider_status = "payout_failure"
            failure_code = re.sub(
                r"[^A-Za-z0-9_.-]", "_", str(data.get("failure_reason_code") or "")
            )[:120]
            failure_description = str(data.get("failure_description") or "")
        else:
            resource = data.get("resource")
            if not isinstance(resource, dict) or resource.get("type") != "transfer":
                raise PaymentDeliveryError("wise_webhook_invalid_resource")
            transfer_id = self._wise_event_positive_id(
                resource.get("id"), "transfer_id"
            )
            profile_id = self._wise_event_positive_id(
                resource.get("profile_id"), "profile_id"
            )
            if event_type == "transfers#refund":
                provider_status = "funds_refunded"
                failure_code = "funds_refunded"
                amount = str(resource.get("refund_amount") or "")[:40]
                currency = str(resource.get("refund_currency") or "")[:8]
                failure_description = f"Wise refund {amount} {currency}".strip()
            else:
                provider_status = str(data.get("current_state") or "").lower()
                if not re.fullmatch(r"[a-z][a-z_]{0,79}", provider_status):
                    raise PaymentDeliveryError("wise_webhook_invalid_state")

        if not self.database.begin_integration_webhook(
            "wise", delivery_id, event_type, datetime.now(UTC)
        ):
            return {"outcome": "duplicate"}
        try:
            if int(profile_id) != self.settings.wise_profile_id:
                result = {"outcome": "wrong_profile"}
                outcome = "ignored"
            else:
                result = self.database.queue_wise_payroll_event(
                    transfer_id,
                    provider_status,
                    occurred_at,
                    failure_code=failure_code,
                    failure_description=failure_description,
                )
                if result is None:
                    result = {"outcome": "unknown_transfer"}
                    outcome = "ignored"
                elif result["outcome"] == "stale":
                    outcome = "ignored"
                else:
                    outcome = "processed"
        except Exception as exc:
            self.database.finish_integration_webhook("wise", delivery_id, "failed")
            raise PaymentDeliveryError("wise_webhook_processing_failed") from exc
        self.database.finish_integration_webhook("wise", delivery_id, outcome)
        return result

    def send(self, payment_id: str) -> dict:
        if not self.configured:
            raise PaymentDeliveryError("Payroll provider is not configured")
        try:
            payment = self.database.claim_payroll_delivery(payment_id, self.provider)
        except ValueError as exc:
            raise PaymentDeliveryError(str(exc)) from exc
        try:
            if self.provider == "paypal":
                return self._send_paypal(payment)
            if self.provider == "wise":
                return self._send_wise(payment)
            return self._send_webhook(payment)
        except PaymentDeliveryError as exc:
            self.database.record_payroll_delivery(
                payment_id,
                "failed",
                provider=self.provider,
                external_reference=str(payment.get("external_reference") or ""),
                failure_reason=str(exc),
            )
            raise

    def _send_webhook(self, payment: dict) -> dict:
        payload = {
            "event": "payroll.payment.requested",
            "idempotency_key": payment["id"],
            "payment": {
                "id": payment["id"],
                "member_id": payment["user_id"],
                "member_email": payment["email"],
                "member_name": payment["full_name"] or payment["email"],
                "period_start": str(payment["period_start"]),
                "period_end": str(payment["period_end"]),
                "amount": str(payment["gross_amount"]),
                "currency": payment["currency"],
            },
        }
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        signature = hmac.new(
            self.settings.payment_webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        request = Request(
            self.settings.payment_webhook_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": payment["id"],
                "X-Dayfinch-Signature": f"sha256={signature}",
                "User-Agent": "Dayfinch-Payroll/1.0",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310 - validated URL
                raw = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
        except (URLError, OSError) as exc:
            raise PaymentDeliveryError("payroll_webhook_unavailable") from exc
        if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
            raise PaymentDeliveryError("payroll_webhook_response_too_large")
        try:
            result = json.loads(raw or b"{}")
        except (TypeError, ValueError) as exc:
            raise PaymentDeliveryError("payroll_webhook_invalid_response") from exc
        if not isinstance(result, dict):
            raise PaymentDeliveryError("payroll_webhook_invalid_response")
        reference = str(result.get("reference") or result.get("id") or "")[:255]
        provider_status = str(result.get("status", "processing")).lower()
        status = (
            provider_status
            if provider_status in {"processing", "paid"}
            else "processing"
        )
        self.database.record_payroll_delivery(
            payment["id"], status, provider="webhook", external_reference=reference
        )
        return self.database.get_payroll_payment(payment["id"]) or {}

    @staticmethod
    def _response_json(response: httpx.Response) -> dict:
        if len(response.content) > MAX_PROVIDER_RESPONSE_BYTES:
            raise PaymentDeliveryError("paypal_response_too_large")
        try:
            payload = response.json()
        except ValueError as exc:
            raise PaymentDeliveryError("paypal_invalid_response") from exc
        if not isinstance(payload, dict):
            raise PaymentDeliveryError("paypal_invalid_response")
        return payload

    def _access_token(self, *, force: bool = False) -> str:
        with self._token_lock:
            if not force and self._token and time.monotonic() < self._token_expires_at:
                return self._token
            try:
                response = self._client.post(
                    "/v1/oauth2/token",
                    data={"grant_type": "client_credentials"},
                    auth=httpx.BasicAuth(
                        self.settings.paypal_client_id,
                        self.settings.paypal_client_secret,
                    ),
                    headers={"Accept-Language": "en_US"},
                )
            except httpx.HTTPError as exc:
                raise PaymentDeliveryError("paypal_auth_unavailable") from exc
            if response.status_code != 200:
                raise PaymentDeliveryError(f"paypal_auth_http_{response.status_code}")
            payload = self._response_json(response)
            token = payload.get("access_token")
            token_type = str(payload.get("token_type", "Bearer"))
            if (
                not isinstance(token, str)
                or not 20 <= len(token) <= 4096
                or token_type.lower() != "bearer"
            ):
                raise PaymentDeliveryError("paypal_invalid_auth_response")
            try:
                expires_in = max(60, min(int(payload.get("expires_in", 300)), 32400))
            except (TypeError, ValueError):
                expires_in = 300
            self._token = token
            self._token_expires_at = time.monotonic() + expires_in - 30
            return token

    def _paypal_request(self, method: str, path: str, **kwargs) -> httpx.Response:
        original_headers = dict(kwargs.pop("headers", {}))
        for attempt in range(2):
            headers = dict(original_headers)
            headers["Authorization"] = f"Bearer {self._access_token(force=attempt > 0)}"
            try:
                response = self._client.request(method, path, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                raise PaymentDeliveryError("paypal_api_unavailable") from exc
            if response.status_code != 401 or attempt == 1:
                return response
        raise PaymentDeliveryError("paypal_auth_failed")

    def _duplicate_reference(self, response: httpx.Response) -> str:
        if response.status_code != 400:
            return ""
        try:
            payload = self._response_json(response)
        except PaymentDeliveryError:
            return ""
        marker = json.dumps(payload, separators=(",", ":"), sort_keys=True).upper()
        if "DUPLICATE" not in marker:
            return ""
        configured = urlparse(self.settings.paypal_api_url)
        for link in payload.get("links", []):
            if not isinstance(link, dict) or str(link.get("rel", "")).lower() != "self":
                continue
            parsed = urlparse(str(link.get("href", "")))
            prefix = "/v1/payments/payouts/"
            if (
                parsed.scheme == configured.scheme
                and parsed.netloc == configured.netloc
                and parsed.path.startswith(prefix)
                and not parsed.query
                and not parsed.fragment
            ):
                reference = parsed.path[len(prefix) :]
                if PAYPAL_REFERENCE.fullmatch(reference):
                    return reference
        return ""

    def _send_paypal(self, payment: dict) -> dict:
        recipient = str(payment.get("recipient") or "")
        if (
            len(recipient) > 127
            or recipient.count("@") != 1
            or any(character.isspace() for character in recipient)
        ):
            raise PaymentDeliveryError("paypal_invalid_recipient")
        currency = str(payment["currency"]).upper()
        if currency not in PAYPAL_CURRENCIES:
            raise PaymentDeliveryError("paypal_unsupported_currency")
        try:
            amount = Decimal(payment["gross_amount"])
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise PaymentDeliveryError("paypal_invalid_amount") from exc
        if amount <= 0:
            raise PaymentDeliveryError("paypal_invalid_amount")
        if currency in PAYPAL_ZERO_DECIMAL_CURRENCIES:
            if amount != amount.quantize(Decimal("1")):
                raise PaymentDeliveryError("paypal_currency_requires_whole_amount")
            amount_value = f"{amount:.0f}"
        else:
            amount_value = f"{amount:.2f}"
        payload = {
            "sender_batch_header": {
                "sender_batch_id": payment["id"],
                "email_subject": "You have a payment from Dayfinch",
                "email_message": "Your approved payroll payment has been sent.",
            },
            "items": [
                {
                    "recipient_type": "EMAIL",
                    "amount": {
                        "value": amount_value,
                        "currency": currency,
                    },
                    "receiver": recipient,
                    "note": (
                        f"Payroll {payment['period_start']} to {payment['period_end']}"
                    ),
                    "sender_item_id": payment["id"],
                    "recipient_wallet": "PAYPAL",
                }
            ],
        }
        response = self._paypal_request(
            "POST",
            "/v1/payments/payouts",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "PayPal-Request-Id": payment["id"],
            },
        )
        if response.status_code not in {200, 201, 202}:
            duplicate = self._duplicate_reference(response)
            if duplicate:
                self.database.record_payroll_delivery(
                    payment["id"],
                    "processing",
                    provider="paypal",
                    external_reference=duplicate,
                )
                return self.database.get_payroll_payment(payment["id"]) or {}
            raise PaymentDeliveryError(f"paypal_payout_http_{response.status_code}")
        result = self._response_json(response)
        header = result.get("batch_header")
        if not isinstance(header, dict):
            raise PaymentDeliveryError("paypal_invalid_payout_response")
        reference = str(header.get("payout_batch_id") or "")
        if not PAYPAL_REFERENCE.fullmatch(reference):
            raise PaymentDeliveryError("paypal_invalid_payout_reference")
        status, reason = self._paypal_status(result)
        self.database.record_payroll_delivery(
            payment["id"],
            status,
            provider="paypal",
            external_reference=reference,
            failure_reason=reason,
        )
        return self.database.get_payroll_payment(payment["id"]) or {}

    @staticmethod
    def _paypal_status(
        payload: dict, *, prior_status: str = "processing"
    ) -> tuple[str, str]:
        items = payload.get("items")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            transaction = str(items[0].get("transaction_status", "")).upper()
            if transaction == "SUCCESS":
                return "paid", ""
            if transaction in {
                "FAILED",
                "RETURNED",
                "BLOCKED",
                "REFUNDED",
                "REVERSED",
            }:
                status = "reversed" if prior_status == "paid" else "failed"
                return status, f"paypal_{transaction.lower()}"
        header = payload.get("batch_header")
        batch = (
            str(header.get("batch_status", "")).upper()
            if isinstance(header, dict)
            else ""
        )
        if batch in {"DENIED", "CANCELED"}:
            status = "reversed" if prior_status == "paid" else "failed"
            return status, f"paypal_{batch.lower()}"
        return "processing", ""

    @staticmethod
    def _wise_json(response: httpx.Response) -> dict:
        if len(response.content) > MAX_PROVIDER_RESPONSE_BYTES:
            raise PaymentDeliveryError("wise_response_too_large")
        try:
            payload = response.json()
        except ValueError as exc:
            raise PaymentDeliveryError("wise_invalid_response") from exc
        if not isinstance(payload, dict):
            raise PaymentDeliveryError("wise_invalid_response")
        return payload

    def _wise_request(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        headers.update(
            {
                "Authorization": f"Bearer {self.settings.wise_api_token}",
                "X-External-Correlation-Id": kwargs.pop("correlation_id"),
            }
        )
        try:
            return self._wise_client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise PaymentDeliveryError("wise_api_unavailable") from exc

    def _send_wise(self, payment: dict) -> dict:
        recipient = str(payment.get("recipient") or "")
        recipient_currency = str(payment.get("recipient_currency") or "").upper()
        currency = str(payment["currency"]).upper()
        if not WISE_TRANSFER_REFERENCE.fullmatch(recipient):
            raise PaymentDeliveryError("wise_invalid_recipient")
        if recipient_currency != currency:
            raise PaymentDeliveryError("wise_recipient_currency_mismatch")
        try:
            amount = Decimal(payment["gross_amount"])
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise PaymentDeliveryError("wise_invalid_amount") from exc
        if amount <= 0:
            raise PaymentDeliveryError("wise_invalid_amount")
        existing_reference = str(payment.get("external_reference") or "")
        if existing_reference:
            return self._fund_wise(payment, existing_reference)

        correlation_id = payment["id"]
        quote_response = self._wise_request(
            "POST",
            f"profiles/{self.settings.wise_profile_id}/quotes",
            correlation_id=correlation_id,
            headers={"Content-Type": "application/json"},
            json={
                "sourceCurrency": self.settings.wise_source_currency,
                "targetCurrency": currency,
                "targetAmount": float(amount),
                "targetAccount": int(recipient),
                "payOut": None,
                "preferredPayIn": "BALANCE",
            },
        )
        if quote_response.status_code not in {200, 201}:
            raise PaymentDeliveryError(f"wise_quote_http_{quote_response.status_code}")
        quote = self._wise_json(quote_response)
        quote_id = str(quote.get("id") or "")
        try:
            UUID(quote_id)
            quoted_amount = Decimal(str(quote["targetAmount"]))
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise PaymentDeliveryError("wise_invalid_quote_response") from exc
        if (
            str(quote.get("targetCurrency", "")).upper() != currency
            or int(quote.get("targetAccount") or 0) != int(recipient)
            or quoted_amount != amount
        ):
            raise PaymentDeliveryError("wise_quote_mismatch")

        transfer_response = self._wise_request(
            "POST",
            "transfers",
            correlation_id=correlation_id,
            headers={"Content-Type": "application/json"},
            json={
                "targetAccount": int(recipient),
                "quoteUuid": quote_id,
                "customerTransactionId": payment["id"],
                "details": {
                    "reference": (
                        f"Dayfinch payroll {payment['period_start']} to "
                        f"{payment['period_end']}"
                    )
                },
            },
        )
        if transfer_response.status_code not in {200, 201}:
            raise PaymentDeliveryError(
                f"wise_transfer_http_{transfer_response.status_code}"
            )
        transfer = self._wise_json(transfer_response)
        reference = str(transfer.get("id") or "")
        if (
            not WISE_TRANSFER_REFERENCE.fullmatch(reference)
            or int(transfer.get("targetAccount") or 0) != int(recipient)
            or str(transfer.get("customerTransactionId") or payment["id"])
            != payment["id"]
        ):
            raise PaymentDeliveryError("wise_invalid_transfer_response")
        self.database.record_payroll_delivery(
            payment["id"],
            "processing",
            provider="wise",
            external_reference=reference,
        )
        payment["external_reference"] = reference
        return self._fund_wise(payment, reference)

    def _fund_wise(self, payment: dict, reference: str) -> dict:
        if not WISE_TRANSFER_REFERENCE.fullmatch(reference):
            raise PaymentDeliveryError("wise_invalid_transfer_reference")
        response = self._wise_request(
            "POST",
            (
                f"profiles/{self.settings.wise_profile_id}/transfers/"
                f"{reference}/payments"
            ),
            correlation_id=payment["id"],
            headers={"Content-Type": "application/json"},
            json={"type": "BALANCE", "balanceId": self.settings.wise_balance_id},
        )
        if response.status_code == 409:
            return self._reconcile_wise(payment)
        if response.status_code == 403:
            raise PaymentDeliveryError("wise_funding_requires_approval")
        if response.status_code not in {200, 201}:
            raise PaymentDeliveryError(f"wise_funding_http_{response.status_code}")
        result = self._wise_json(response)
        if str(result.get("status", "")).upper() != "COMPLETED":
            error_code = str(result.get("errorCode") or "rejected")
            safe_code = re.sub(r"[^a-z0-9_.-]", "_", error_code.lower())[:80]
            raise PaymentDeliveryError(f"wise_funding_{safe_code}")
        self.database.record_payroll_delivery(
            payment["id"],
            "processing",
            provider="wise",
            external_reference=reference,
        )
        return self.database.get_payroll_payment(payment["id"]) or {}

    @staticmethod
    def _wise_status(
        payload: dict, *, prior_status: str = "processing"
    ) -> tuple[str, str]:
        provider_status = str(payload.get("status", "")).lower()
        if provider_status == "outgoing_payment_sent":
            return "paid", ""
        if provider_status in {"funds_refunded", "charged_back"}:
            status = "reversed" if prior_status == "paid" else "failed"
            return status, f"wise_{provider_status}"
        if provider_status == "cancelled":
            status = "reversed" if prior_status == "paid" else "failed"
            return status, f"wise_{provider_status}"
        return "processing", ""

    def _reconcile_wise(self, payment: dict) -> dict:
        reference = str(payment.get("external_reference") or "")
        if not WISE_TRANSFER_REFERENCE.fullmatch(reference):
            raise PaymentDeliveryError("Wise transfer reference is unavailable")
        response = self._wise_request(
            "GET",
            f"transfers/{reference}",
            correlation_id=payment["id"],
        )
        if response.status_code != 200:
            raise PaymentDeliveryError(f"wise_reconcile_http_{response.status_code}")
        payload = self._wise_json(response)
        if str(payload.get("id") or "") != reference:
            raise PaymentDeliveryError("wise_invalid_transfer_response")
        status, reason = self._wise_status(
            payload, prior_status=str(payment.get("status") or "processing")
        )
        try:
            self.database.record_payroll_delivery(
                payment["id"],
                status,
                provider="wise",
                external_reference=reference,
                failure_reason=reason,
            )
        except ValueError as exc:
            raise PaymentDeliveryError("payroll_reconciliation_conflict") from exc
        return self.database.get_payroll_payment(payment["id"]) or {}

    def reconcile(self, payment_id: str) -> dict:
        payment = self.database.get_payroll_payment(payment_id)
        if not payment:
            raise PaymentDeliveryError("Payroll payment not found")
        if payment["provider"] == "wise":
            return self._reconcile_wise(payment)
        if payment["provider"] != "paypal":
            raise PaymentDeliveryError("Only direct-provider payroll is reconciled")
        reference = str(payment["external_reference"] or "")
        if not PAYPAL_REFERENCE.fullmatch(reference):
            raise PaymentDeliveryError("PayPal payout reference is unavailable")
        response = self._paypal_request(
            "GET",
            f"/v1/payments/payouts/{reference}",
            params={"page": 1, "page_size": 1, "total_required": "true"},
        )
        if response.status_code != 200:
            raise PaymentDeliveryError(f"paypal_reconcile_http_{response.status_code}")
        payload = self._response_json(response)
        status, reason = self._paypal_status(
            payload, prior_status=str(payment.get("status") or "processing")
        )
        try:
            self.database.record_payroll_delivery(
                payment_id,
                status,
                provider="paypal",
                external_reference=reference,
                failure_reason=reason,
            )
        except ValueError as exc:
            raise PaymentDeliveryError("payroll_reconciliation_conflict") from exc
        return self.database.get_payroll_payment(payment_id) or {}

    def reconcile_due(self) -> int:
        if not self.configured:
            return 0
        reconciled = self.database.recover_stale_payroll_claims(self.provider)
        if self.provider not in {"paypal", "wise"}:
            return reconciled
        for payment in self.database.reconcilable_payroll_payments(self.provider):
            try:
                self.reconcile(payment["id"])
            except PaymentDeliveryError as exc:
                try:
                    self.database.defer_payroll_reconciliation(payment["id"], str(exc))
                except ValueError:
                    LOGGER.exception(
                        "payroll_reconciliation_backoff_failed",
                        extra={"payment_id": payment["id"]},
                    )
                LOGGER.warning(
                    f"{self.provider}_reconciliation_deferred",
                    extra={"payment_id": payment["id"], "reason": str(exc)},
                )
            else:
                reconciled += 1
        return reconciled

    def handle_callback(self, body: bytes, signature: str) -> dict:
        if self.provider != "webhook" or not self.configured:
            raise PaymentDeliveryError("Payroll webhook provider is not configured")
        if len(body) > MAX_PROVIDER_RESPONSE_BYTES:
            raise PaymentDeliveryError("Invalid payroll callback payload")
        expected = (
            "sha256="
            + hmac.new(
                self.settings.payment_webhook_secret.encode(), body, hashlib.sha256
            ).hexdigest()
        )
        if not hmac.compare_digest(expected, signature):
            raise PaymentDeliveryError("Invalid payroll callback signature")
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise TypeError
            payment_id = str(payload["payment_id"])
            status = str(payload["status"]).lower()
        except (KeyError, TypeError, ValueError) as exc:
            raise PaymentDeliveryError("Invalid payroll callback payload") from exc
        if status not in {"processing", "paid", "failed"}:
            raise PaymentDeliveryError("Invalid payroll callback status")
        try:
            self.database.record_payroll_delivery(
                payment_id,
                status,
                provider="webhook",
                external_reference=str(payload.get("reference", "")),
                failure_reason=str(payload.get("failure_reason", "")),
            )
        except ValueError as exc:
            raise PaymentDeliveryError(str(exc)) from exc
        return self.database.get_payroll_payment(payment_id) or {}

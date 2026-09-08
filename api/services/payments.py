from __future__ import annotations

import hashlib
import hmac
import json
from urllib.error import URLError
from urllib.request import Request, urlopen

from ..config import Settings
from ..database import Database


class PaymentDeliveryError(RuntimeError):
    pass


class PayrollDeliveryService:
    """Dispatch approved payroll through an operator-controlled payment adapter."""

    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.payment_webhook_url and self.settings.payment_webhook_secret
        )

    def send(self, payment_id: str) -> dict:
        if not self.configured:
            raise PaymentDeliveryError("Payroll provider is not configured")
        payment = self.database.get_payroll_payment(payment_id)
        if not payment:
            raise PaymentDeliveryError("Payroll payment not found")
        if payment["status"] not in {"draft", "failed"}:
            raise PaymentDeliveryError("Only draft or failed payroll can be dispatched")

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
        self.database.record_payroll_delivery(payment_id, "processing")
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
            with urlopen(request, timeout=30) as response:  # noqa: S310 - operator-configured URL
                result = json.loads(response.read(1024 * 1024) or b"{}")
                reference = str(result.get("reference") or result.get("id") or "")
        except (URLError, OSError, ValueError) as exc:
            self.database.record_payroll_delivery(
                payment_id, "failed", failure_reason=str(exc)
            )
            raise PaymentDeliveryError(
                "The payroll provider rejected the request"
            ) from exc
        provider_status = str(result.get("status", "processing")).lower()
        status = (
            provider_status
            if provider_status in {"processing", "paid"}
            else "processing"
        )
        self.database.record_payroll_delivery(
            payment_id, status, external_reference=reference
        )
        return self.database.get_payroll_payment(payment_id) or {}

    def handle_callback(self, body: bytes, signature: str) -> dict:
        if not self.configured:
            raise PaymentDeliveryError("Payroll provider is not configured")
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
                external_reference=str(payload.get("reference", "")),
                failure_reason=str(payload.get("failure_reason", "")),
            )
        except ValueError as exc:
            raise PaymentDeliveryError(str(exc)) from exc
        return self.database.get_payroll_payment(payment_id) or {}

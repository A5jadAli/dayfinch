from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from ..config import Settings


class InvitationDeliveryError(RuntimeError):
    """A configured SMTP server could not accept an invitation message."""


class InvitationDeliveryService:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.smtp_host and self.settings.smtp_from_email)

    def deliver(self, recipient: str, invitation_url: str, expires_at: str) -> bool:
        if not self.configured:
            return False

        message = EmailMessage()
        message["Subject"] = "You have been invited to Dayfinch"
        message["From"] = self.settings.smtp_from_email
        message["To"] = recipient
        message.set_content(
            "\n".join(
                (
                    "You have been invited to join a Dayfinch workspace.",
                    "",
                    f"Accept the invitation: {invitation_url}",
                    f"This one-time link expires at {expires_at}.",
                    "",
                    "If you were not expecting this invitation, ignore this email.",
                )
            )
        )

        try:
            with smtplib.SMTP(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=self.settings.smtp_timeout_seconds,
            ) as smtp:
                if self.settings.smtp_starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if self.settings.smtp_username:
                    smtp.login(self.settings.smtp_username, self.settings.smtp_password)
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException, ValueError) as exc:
            raise InvitationDeliveryError("SMTP delivery failed") from exc
        return True

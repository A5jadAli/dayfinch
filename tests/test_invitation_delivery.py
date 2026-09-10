import smtplib

import pytest

from api.config import Settings
from api.services.invitation_delivery import (
    InvitationDeliveryError,
    InvitationDeliveryService,
)


def _settings(tmp_path, **overrides):
    values = {
        "data_dir": tmp_path,
        "admin_password": "correct horse battery staple",
        "session_secret": "s" * 40,
        "cookie_secure": False,
        "max_upload_bytes": 1024,
        "retention_days": 30,
    }
    values.update(overrides)
    return Settings(**values)


def test_invitation_delivery_is_disabled_without_smtp(tmp_path):
    service = InvitationDeliveryService(_settings(tmp_path))
    assert not service.configured
    assert not service.deliver(
        "member@example.test", "https://tracker.example.test/invite/token", "soon"
    )


def test_invitation_delivery_uses_tls_and_authentication(tmp_path, monkeypatch):
    events = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            events.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            events.append(("close",))

        def starttls(self, *, context):
            assert context is not None
            events.append(("tls",))

        def login(self, username, password):
            events.append(("login", username, password))

        def send_message(self, message):
            events.append(
                ("send", message["To"], message["Subject"], message.get_content())
            )

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    service = InvitationDeliveryService(
        _settings(
            tmp_path,
            smtp_host="smtp.example.test",
            smtp_port=587,
            smtp_username="mailer",
            smtp_password="secret",
            smtp_from_email="dayfinch@example.test",
            smtp_timeout_seconds=7,
        )
    )

    assert service.deliver(
        "member@example.test",
        "https://tracker.example.test/invite/one-time",
        "2026-09-15T00:00:00Z",
    )
    assert events[0] == ("connect", "smtp.example.test", 587, 7)
    assert ("tls",) in events
    assert ("login", "mailer", "secret") in events
    sent = next(event for event in events if event[0] == "send")
    assert sent[1] == "member@example.test"
    assert "one-time" in sent[3]


def test_invitation_delivery_reports_smtp_failure(tmp_path, monkeypatch):
    class BrokenSMTP:
        def __init__(self, *_args, **_kwargs):
            raise OSError("network unavailable")

    monkeypatch.setattr(smtplib, "SMTP", BrokenSMTP)
    service = InvitationDeliveryService(
        _settings(
            tmp_path,
            smtp_host="smtp.example.test",
            smtp_from_email="dayfinch@example.test",
        )
    )
    with pytest.raises(InvitationDeliveryError, match="SMTP delivery failed"):
        service.deliver(
            "member@example.test",
            "https://tracker.example.test/invite/token",
            "soon",
        )

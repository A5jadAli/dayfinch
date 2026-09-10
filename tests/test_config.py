import base64
from pathlib import Path

import pytest

from agent.config import AgentConfig
from api.config import Settings

TOKEN = "x" * 40


def config(**overrides):
    values = {
        "server_url": "https://tracker.example.test",
        "device_token": TOKEN,
        "consent_confirmed": True,
        "queue_dir": Path("queue"),
    }
    values.update(overrides)
    return AgentConfig(**values)


def test_consent_is_required():
    with pytest.raises(ValueError, match="consent"):
        config(consent_confirmed=False).validate()


def test_plain_http_is_local_only():
    with pytest.raises(ValueError, match="HTTPS"):
        config(server_url="http://tracker.example.test").validate()
    config(server_url="http://127.0.0.1:8000").validate()


def test_capture_interval_has_safe_minimum():
    with pytest.raises(ValueError, match="capture_interval"):
        config(capture_interval_seconds=10).validate()


def test_agent_project_and_task_ids_must_be_uuids():
    with pytest.raises(ValueError, match="project_id"):
        config(project_id="not-a-project").validate()
    with pytest.raises(ValueError, match="task_id"):
        config(task_id="not-a-task").validate()
    config(project_id="11111111-1111-4111-8111-111111111111").validate()


def test_agent_update_channel_requires_paired_valid_settings():
    public_key = base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode()
    with pytest.raises(ValueError, match="configured together"):
        config(
            update_manifest_url="https://releases.example.test/update.json"
        ).validate()
    with pytest.raises(ValueError, match="base64url"):
        config(
            update_manifest_url="https://releases.example.test/update.json",
            update_public_key="not a key!",
        ).validate()
    with pytest.raises(ValueError, match="requires HTTPS"):
        config(
            update_manifest_url="http://releases.example.test/update.json",
            update_public_key=public_key,
        ).validate()
    config(
        update_manifest_url="https://releases.example.test/update.json",
        update_public_key=public_key,
        update_mode="download",
    ).validate()


def test_server_rejects_non_postgresql_database_url(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024,
        retention_days=30,
        database_url="sqlite:///tracker.db",
    )
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        settings.prepare()


def test_observability_settings_reject_unsafe_values(tmp_path):
    base = dict(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024,
        retention_days=30,
    )
    with pytest.raises(RuntimeError, match="TRACKER_LOG_LEVEL"):
        Settings(**base, log_level="VERBOSE").prepare()
    with pytest.raises(RuntimeError, match="TRACKER_METRICS_BEARER_TOKEN"):
        Settings(**base, metrics_bearer_token="short").prepare()


def test_server_validates_provider_credential_keyring(tmp_path):
    base_settings = dict(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024,
        retention_days=30,
    )
    with pytest.raises(RuntimeError, match="INTEGRATION_ENCRYPTION_KEYS"):
        Settings(
            **base_settings, integration_encryption_keys="primary:not-base64!"
        ).prepare()
    encoded = base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")
    Settings(
        **base_settings,
        integration_encryption_keys=f"2026-09:{encoded}",
    ).prepare()


def test_paypal_payroll_configuration_is_paired_and_live_in_production(tmp_path):
    base = dict(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=True,
        max_upload_bytes=1024,
        retention_days=30,
        admin_email="owner@example.test",
        public_url="https://tracker.example.test",
        allowed_hosts=("tracker.example.test",),
    )
    with pytest.raises(
        RuntimeError, match="CLIENT_ID and TRACKER_PAYPAL_CLIENT_SECRET"
    ):
        Settings(
            **base,
            payment_provider="paypal",
            paypal_client_id="client-only",
        ).prepare()
    sandbox = Settings(
        **base,
        payment_provider="paypal",
        paypal_client_id="client-id",
        paypal_client_secret="client-secret",
    )
    sandbox.prepare()
    with pytest.raises(RuntimeError, match="Production PayPal"):
        sandbox.validate_for_nonlocal()
    live = Settings(
        **base,
        payment_provider="paypal",
        paypal_client_id="client-id",
        paypal_client_secret="client-secret",
        paypal_api_url="https://api-m.paypal.com",
    )
    live.prepare()
    live.validate_for_nonlocal()


def test_payroll_webhook_requires_complete_strong_configuration(tmp_path):
    base = dict(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024,
        retention_days=30,
    )
    with pytest.raises(RuntimeError, match="configured together"):
        Settings(
            **base,
            payment_provider="webhook",
            payment_webhook_url="https://payments.example.test/payroll",
        ).prepare()
    with pytest.raises(RuntimeError, match="at least 32"):
        Settings(
            **base,
            payment_provider="webhook",
            payment_webhook_url="https://payments.example.test/payroll",
            payment_webhook_secret="too-short",
        ).prepare()


def test_wise_payroll_requires_complete_versioned_live_configuration(tmp_path):
    base = dict(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=True,
        max_upload_bytes=1024,
        retention_days=30,
        admin_email="owner@example.test",
        public_url="https://tracker.example.test",
        allowed_hosts=("tracker.example.test",),
        payment_provider="wise",
    )
    with pytest.raises(RuntimeError, match="positive profile ID"):
        Settings(**base, wise_api_token="token-long-enough-for-wise").prepare()
    sandbox = Settings(
        **base,
        wise_api_token="token-long-enough-for-wise",
        wise_profile_id=101,
        wise_balance_id=202,
        wise_source_currency="GBP",
    )
    sandbox.prepare()
    with pytest.raises(RuntimeError, match="Production Wise"):
        sandbox.validate_for_nonlocal()
    live = Settings(
        **base,
        wise_api_token="token-long-enough-for-wise",
        wise_profile_id=101,
        wise_balance_id=202,
        wise_source_currency="GBP",
        wise_api_url="https://api.wise.com/2026Q3",
    )
    live.prepare()
    live.validate_for_nonlocal()
    with pytest.raises(RuntimeError, match="base64-encoded PEM"):
        Settings(
            **base,
            wise_api_token="token-long-enough-for-wise",
            wise_profile_id=101,
            wise_balance_id=202,
            wise_source_currency="GBP",
            wise_webhook_public_key_b64="not-base64!",
        ).prepare()


def test_asana_oauth_configuration_is_paired_encrypted_and_https_in_production(
    tmp_path,
):
    base = dict(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=True,
        max_upload_bytes=1024,
        retention_days=30,
        admin_email="admin@example.test",
        public_url="https://tracker.example.test",
        allowed_hosts=("tracker.example.test",),
    )
    with pytest.raises(RuntimeError, match="configured together"):
        Settings(**base, asana_client_id="client-id").prepare()
    with pytest.raises(RuntimeError, match="INTEGRATION_ENCRYPTION_KEYS"):
        Settings(
            **base,
            asana_client_id="client-id",
            asana_client_secret="client-secret",
        ).prepare()
    encoded = base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")
    with pytest.raises(RuntimeError, match="ASANA_API_URL"):
        Settings(
            **base,
            integration_encryption_keys=f"primary:{encoded}",
            asana_client_id="client-id",
            asana_client_secret="client-secret",
            asana_api_url="https://app.asana.test/api/1.0?token=unsafe",
        ).prepare()
    insecure = Settings(
        **base,
        integration_encryption_keys=f"primary:{encoded}",
        asana_client_id="client-id",
        asana_client_secret="client-secret",
        asana_api_url="http://app.asana.test/api/1.0",
    )
    insecure.prepare()
    with pytest.raises(RuntimeError, match="Asana OAuth and API URLs"):
        insecure.validate_for_nonlocal()


def test_slack_oauth_configuration_is_paired_encrypted_and_https_in_production(
    tmp_path,
):
    base = dict(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=True,
        max_upload_bytes=1024,
        retention_days=30,
        admin_email="admin@example.test",
        public_url="https://tracker.example.test",
        allowed_hosts=("tracker.example.test",),
    )
    with pytest.raises(RuntimeError, match="configured together"):
        Settings(**base, slack_client_id="client-id").prepare()
    with pytest.raises(RuntimeError, match="INTEGRATION_ENCRYPTION_KEYS"):
        Settings(
            **base,
            slack_client_id="client-id",
            slack_client_secret="client-secret",
        ).prepare()
    encoded = base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")
    with pytest.raises(RuntimeError, match="SLACK_API_URL"):
        Settings(
            **base,
            integration_encryption_keys=f"primary:{encoded}",
            slack_client_id="client-id",
            slack_client_secret="client-secret",
            slack_api_url="https://slack.test/api?token=unsafe",
        ).prepare()
    insecure = Settings(
        **base,
        integration_encryption_keys=f"primary:{encoded}",
        slack_client_id="client-id",
        slack_client_secret="client-secret",
        slack_api_url="http://slack.test/api",
    )
    insecure.prepare()
    with pytest.raises(RuntimeError, match="Slack OAuth and API URLs"):
        insecure.validate_for_nonlocal()


def test_public_url_must_be_a_safe_origin(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024,
        retention_days=30,
        public_url="https://tracker.example.test/path?token=bad",
    )
    with pytest.raises(RuntimeError, match="TRACKER_PUBLIC_URL"):
        settings.prepare()


def test_audit_retention_has_safe_compliance_bounds(tmp_path):
    base = dict(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024,
        retention_days=30,
    )
    with pytest.raises(RuntimeError, match="AUDIT_RETENTION_DAYS"):
        Settings(**base, audit_retention_days=29).prepare()
    with pytest.raises(RuntimeError, match="AUDIT_RETENTION_DAYS"):
        Settings(**base, audit_retention_days=3651).prepare()
    Settings(**base, audit_retention_days=2555).prepare()


def test_server_update_channel_requires_manifest_and_ed25519_public_key(tmp_path):
    base = dict(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024,
        retention_days=30,
    )
    with pytest.raises(RuntimeError, match="configured together"):
        Settings(
            **base,
            agent_update_manifest_url="https://releases.example.test/update.json",
        ).prepare()
    with pytest.raises(RuntimeError, match="decode to 32 bytes"):
        Settings(
            **base,
            agent_update_manifest_url="https://releases.example.test/update.json",
            agent_update_public_key=base64.urlsafe_b64encode(b"short")
            .rstrip(b"=")
            .decode(),
        ).prepare()
    public_key = base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode()
    with pytest.raises(RuntimeError, match="requires HTTPS"):
        Settings(
            **base,
            agent_update_manifest_url="http://releases.example.test/update.json",
            agent_update_public_key=public_key,
        ).prepare()


def test_nonlocal_configuration_requires_https_public_url(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=True,
        max_upload_bytes=1024,
        retention_days=30,
        admin_email="admin@example.test",
        public_url="http://tracker.example.test",
    )
    settings.prepare()
    with pytest.raises(RuntimeError, match="HTTPS"):
        settings.validate_for_nonlocal()


def test_production_app_settings_require_secure_cookie_and_matching_host(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024,
        retention_days=30,
        admin_email="admin@example.test",
        environment="production",
        public_url="https://tracker.example.test",
        allowed_hosts=("tracker.example.test",),
    )
    settings.prepare()
    with pytest.raises(RuntimeError, match="COOKIE_SECURE"):
        settings.validate_for_nonlocal()

    wrong_host = Settings(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=True,
        max_upload_bytes=1024,
        retention_days=30,
        admin_email="admin@example.test",
        environment="production",
        public_url="https://tracker.example.test",
        allowed_hosts=("other.example.test",),
    )
    wrong_host.prepare()
    with pytest.raises(RuntimeError, match="PUBLIC_URL hostname"):
        wrong_host.validate_for_nonlocal()

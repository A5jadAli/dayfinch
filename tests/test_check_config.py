from __future__ import annotations

import ast
import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from scripts.check_config import (
    CATEGORIES,
    RUNTIME_SETTINGS,
    check_config,
    main,
    render,
)


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode()


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _certificate_bundle(password: str) -> tuple[str, str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Dayfinch test")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM)
    bundle = pkcs12.serialize_key_and_certificates(
        b"dayfinch",
        key,
        certificate,
        None,
        serialization.BestAvailableEncryption(password.encode()),
    )
    return _b64(key_pem), _b64(certificate_pem), _b64(bundle)


def _valid_environment() -> dict[str, str]:
    saml_key, saml_certificate, _unused = _certificate_bundle("saml-password")
    _windows_key, _windows_certificate, windows_pfx = _certificate_bundle(
        "windows-password"
    )
    _app_key, _app_certificate, application_p12 = _certificate_bundle("mac-password")
    _installer_key, _installer_certificate, installer_p12 = _certificate_bundle(
        "mac-password"
    )
    github_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    github_pem = github_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    notary_key = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    update_seed = b"u" * 32
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    update_public = Ed25519PrivateKey.from_private_bytes(update_seed).public_key()
    update_public_bytes = update_public.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    credential_key = _b64url(b"i" * 32)
    return {
        "TRACKER_ENVIRONMENT": "production",
        "TRACKER_PUBLIC_URL": "https://tracker.example.test",
        "TRACKER_ALLOWED_HOSTS": "tracker.example.test",
        "TRACKER_COOKIE_SECURE": "true",
        "TRACKER_DATABASE_URL": "postgresql://dayfinch:secret@db.example.test/dayfinch",
        "POSTGRES_PASSWORD": "database-password",
        "TRACKER_ADMIN_EMAIL": "owner@example.test",
        "TRACKER_ADMIN_PASSWORD": "admin-password-long-enough",
        "TRACKER_SESSION_SECRET": "s" * 40,
        "TRACKER_DOCUMENT_ENCRYPTION_KEY": _b64url(b"d" * 32),
        "TRACKER_INTEGRATION_ENCRYPTION_KEYS": f"primary:{credential_key}",
        "TRACKER_DATA_DIR": "/data",
        "TRACKER_SMTP_HOST": "smtp.example.test",
        "TRACKER_SMTP_PORT": "587",
        "TRACKER_SMTP_USERNAME": "dayfinch",
        "TRACKER_SMTP_PASSWORD": "smtp-password",
        "TRACKER_SMTP_FROM_EMAIL": "dayfinch@example.test",
        "TRACKER_SMTP_STARTTLS": "true",
        "TRACKER_SMTP_TIMEOUT_SECONDS": "10",
        "TRACKER_STORAGE_BACKEND": "s3",
        "TRACKER_S3_BUCKET": "dayfinch-production",
        "TRACKER_S3_REGION": "us-east-1",
        "TRACKER_S3_SSE": "AES256",
        "AWS_ACCESS_KEY_ID": "access-key-id",
        "AWS_SECRET_ACCESS_KEY": "storage-secret",
        "TRACKER_OIDC_ISSUER": "https://id.example.test",
        "TRACKER_OIDC_CLIENT_ID": "oidc-client",
        "TRACKER_OIDC_CLIENT_SECRET": "oidc-secret",
        "TRACKER_OIDC_CLIENT_AUTH_METHOD": "client_secret_basic",
        "TRACKER_SAML_IDP_ENTITY_ID": "https://id.example.test/saml",
        "TRACKER_SAML_IDP_METADATA_B64": _b64(b"<EntityDescriptor/>"),
        "TRACKER_SAML_SP_PRIVATE_KEY_B64": saml_key,
        "TRACKER_SAML_SP_CERTIFICATE_B64": saml_certificate,
        "TRACKER_SAML_EMAIL_ATTRIBUTE": "email",
        "TRACKER_SCIM_BEARER_TOKEN": "c" * 40,
        "TRACKER_AGENT_WINDOWS_URL": "https://downloads.example.test/dayfinch.exe",
        "TRACKER_AGENT_MACOS_URL": "https://downloads.example.test/dayfinch.pkg",
        "TRACKER_AGENT_LINUX_URL": "https://downloads.example.test/dayfinch.deb",
        "TRACKER_AGENT_UPDATE_MANIFEST_URL": "https://downloads.example.test/update.json",
        "TRACKER_AGENT_UPDATE_PUBLIC_KEY": _b64url(update_public_bytes),
        "DAYFINCH_UPDATE_SIGNING_KEY": _b64url(update_seed),
        "WINDOWS_CERTIFICATE_PFX": windows_pfx,
        "WINDOWS_CERTIFICATE_PASSWORD": "windows-password",
        "WINDOWS_TIMESTAMP_URL": "https://timestamp.example.test",
        "MACOS_APPLICATION_CERTIFICATE_P12": application_p12,
        "MACOS_INSTALLER_CERTIFICATE_P12": installer_p12,
        "MACOS_CERTIFICATE_PASSWORD": "mac-password",
        "MACOS_APPLICATION_IDENTITY": "Developer ID Application: Example",
        "MACOS_INSTALLER_IDENTITY": "Developer ID Installer: Example",
        "MACOS_NOTARY_API_KEY_P8": _b64(notary_key),
        "MACOS_NOTARY_KEY_ID": "A1B2C3D4E5",
        "MACOS_NOTARY_ISSUER": "11111111-2222-4333-8444-555555555555",
        "TRACKER_GITHUB_APP_SLUG": "dayfinch-test",
        "TRACKER_GITHUB_CLIENT_ID": "github-client",
        "TRACKER_GITHUB_CLIENT_SECRET": "github-secret",
        "TRACKER_GITHUB_PRIVATE_KEY_B64": _b64(github_pem),
        "TRACKER_GITHUB_WEBHOOK_SECRET": "g" * 40,
        "TRACKER_GITHUB_API_URL": "https://api.github.com",
        "TRACKER_GITHUB_WEB_URL": "https://github.com",
        "TRACKER_GITHUB_API_VERSION": "2026-03-10",
        "TRACKER_JIRA_CLIENT_ID": "jira-client",
        "TRACKER_JIRA_CLIENT_SECRET": "jira-secret",
        "TRACKER_JIRA_AUTHORIZE_URL": "https://auth.atlassian.com/authorize",
        "TRACKER_JIRA_TOKEN_URL": "https://auth.atlassian.com/oauth/token",
        "TRACKER_JIRA_API_URL": "https://api.atlassian.com",
        "TRACKER_ASANA_CLIENT_ID": "asana-client",
        "TRACKER_ASANA_CLIENT_SECRET": "asana-secret",
        "TRACKER_ASANA_AUTHORIZE_URL": "https://app.asana.com/-/oauth_authorize",
        "TRACKER_ASANA_TOKEN_URL": "https://app.asana.com/-/oauth_token",
        "TRACKER_ASANA_TOKEN_INFO_URL": "https://app.asana.com/-/token_info",
        "TRACKER_ASANA_REVOKE_URL": "https://app.asana.com/-/oauth_revoke",
        "TRACKER_ASANA_API_URL": "https://app.asana.com/api/1.0",
        "TRACKER_SLACK_CLIENT_ID": "slack-client",
        "TRACKER_SLACK_CLIENT_SECRET": "slack-secret",
        "TRACKER_SLACK_AUTHORIZE_URL": "https://slack.com/oauth/v2/authorize",
        "TRACKER_SLACK_TOKEN_URL": "https://slack.com/api/oauth.v2.access",
        "TRACKER_SLACK_API_URL": "https://slack.com/api",
        "TRACKER_PAYMENT_PROVIDER": "manual",
        "TRACKER_BACKUP_ENCRYPTION_KEY": _b64url(b"b" * 32),
        "DAYFINCH_BACKUP_DESTINATION": "/var/backups/dayfinch",
        "TRACKER_METRICS_BEARER_TOKEN": "m" * 40,
    }


def test_complete_production_configuration_passes_format_preflight():
    assert check_config(_valid_environment()) == []


def test_findings_name_variables_without_disclosing_values():
    secret_value = "do-not-leak-this-secret"
    findings = check_config(
        {"TRACKER_SESSION_SECRET": secret_value}, categories=("core",)
    )
    output = render(findings, ("core",))
    assert "TRACKER_SESSION_SECRET" in output
    assert secret_value not in output
    assert "[MISSING/INVALID] core" in output


def test_category_cli_and_json_are_value_free(tmp_path, capsys):
    secret_value = "monitoring-secret-that-must-never-print"
    environment = tmp_path / "production.env"
    environment.write_text(
        f"TRACKER_METRICS_BEARER_TOKEN={secret_value}\n", encoding="utf-8"
    )
    assert (
        main(["--env-file", str(environment), "--category", "monitoring", "--json"])
        == 0
    )
    payload = capsys.readouterr().out
    assert json.loads(payload)["ready"] is True
    assert secret_value not in payload


def test_invalid_env_file_reports_only_the_line_number(tmp_path, capsys):
    environment = tmp_path / "broken.env"
    environment.write_text(
        "not an assignment containing secret-data\n", encoding="utf-8"
    )
    assert main(["--env-file", str(environment)]) == 2
    error = capsys.readouterr().err
    assert "line 1" in error
    assert "secret-data" not in error


def test_update_signing_pair_must_match():
    environment = _valid_environment()
    environment["TRACKER_AGENT_UPDATE_PUBLIC_KEY"] = _b64url(b"x" * 32)
    findings = check_config(environment, categories=("signing",))
    assert any(
        finding.variable == "TRACKER_AGENT_UPDATE_PUBLIC_KEY"
        and "match" in finding.problem
        for finding in findings
    )


def test_runtime_setting_inventory_covers_settings_from_env():
    source = Path("api/config.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    used = {
        call.args[0].value
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "os"
        and call.func.attr == "getenv"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
    }
    assert used <= RUNTIME_SETTINGS
    assert tuple(CATEGORIES) == (
        "core",
        "smtp",
        "s3",
        "sso",
        "signing",
        "integrations",
        "backups",
        "monitoring",
    )

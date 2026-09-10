"""Read-only production configuration preflight.

The checker validates presence and local format only. It never connects to a
provider and never prints configured values, including values that fail parsing.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from cryptography.hazmat.primitives.serialization import pkcs12

from api.services.integration_credentials import CredentialKeyring

CATEGORIES = (
    "core",
    "smtp",
    "s3",
    "sso",
    "signing",
    "integrations",
    "backups",
    "monitoring",
)

# Keep this inventory in lockstep with Settings.from_env. A test compares it to
# the source so a newly introduced production knob cannot silently bypass the
# handoff checker.
RUNTIME_SETTINGS = frozenset(
    {
        "TRACKER_ADMIN_EMAIL",
        "TRACKER_ADMIN_PASSWORD",
        "TRACKER_AGENT_LINUX_URL",
        "TRACKER_AGENT_MACOS_URL",
        "TRACKER_AGENT_UPDATE_MANIFEST_URL",
        "TRACKER_AGENT_UPDATE_PUBLIC_KEY",
        "TRACKER_AGENT_WINDOWS_URL",
        "TRACKER_ALLOWED_HOSTS",
        "TRACKER_ANONYMOUS_REQUEST_LIMIT",
        "TRACKER_ASANA_API_URL",
        "TRACKER_ASANA_AUTHORIZE_URL",
        "TRACKER_ASANA_CLIENT_ID",
        "TRACKER_ASANA_CLIENT_SECRET",
        "TRACKER_ASANA_REVOKE_URL",
        "TRACKER_ASANA_TOKEN_INFO_URL",
        "TRACKER_ASANA_TOKEN_URL",
        "TRACKER_AUDIT_RETENTION_DAYS",
        "TRACKER_COOKIE_SECURE",
        "TRACKER_DATABASE_MAX_POOL_SIZE",
        "TRACKER_DATABASE_MIN_POOL_SIZE",
        "TRACKER_DATABASE_URL",
        "TRACKER_DATA_DIR",
        "TRACKER_DEVICE_REPLAY_REQUEST_LIMIT",
        "TRACKER_DEVICE_REQUEST_LIMIT",
        "TRACKER_DOCUMENT_ENCRYPTION_KEY",
        "TRACKER_ENVIRONMENT",
        "TRACKER_GITHUB_API_URL",
        "TRACKER_GITHUB_API_VERSION",
        "TRACKER_GITHUB_APP_SLUG",
        "TRACKER_GITHUB_CLIENT_ID",
        "TRACKER_GITHUB_CLIENT_SECRET",
        "TRACKER_GITHUB_PRIVATE_KEY_B64",
        "TRACKER_GITHUB_WEBHOOK_SECRET",
        "TRACKER_GITHUB_WEB_URL",
        "TRACKER_INTEGRATION_ENCRYPTION_KEYS",
        "TRACKER_INVITATION_HOURS",
        "TRACKER_JIRA_API_URL",
        "TRACKER_JIRA_AUTHORIZE_URL",
        "TRACKER_JIRA_CLIENT_ID",
        "TRACKER_JIRA_CLIENT_SECRET",
        "TRACKER_JIRA_TOKEN_URL",
        "TRACKER_LOGIN_IDENTITY_FAILURE_LIMIT",
        "TRACKER_LOGIN_SOURCE_FAILURE_LIMIT",
        "TRACKER_LOGIN_WINDOW_MINUTES",
        "TRACKER_LOG_LEVEL",
        "TRACKER_MAX_UPLOAD_MB",
        "TRACKER_METRICS_BEARER_TOKEN",
        "TRACKER_OIDC_CLIENT_AUTH_METHOD",
        "TRACKER_OIDC_CLIENT_ID",
        "TRACKER_OIDC_CLIENT_SECRET",
        "TRACKER_OIDC_DISCOVERY_URL",
        "TRACKER_OIDC_END_SESSION_URL",
        "TRACKER_OIDC_ISSUER",
        "TRACKER_PAYMENT_PROVIDER",
        "TRACKER_PAYMENT_WEBHOOK_SECRET",
        "TRACKER_PAYMENT_WEBHOOK_URL",
        "TRACKER_PAYPAL_API_URL",
        "TRACKER_PAYPAL_CLIENT_ID",
        "TRACKER_PAYPAL_CLIENT_SECRET",
        "TRACKER_PUBLIC_URL",
        "TRACKER_RATE_LIMIT_WINDOW_SECONDS",
        "TRACKER_RETENTION_DAYS",
        "TRACKER_S3_BUCKET",
        "TRACKER_S3_ENDPOINT_URL",
        "TRACKER_S3_KMS_KEY_ID",
        "TRACKER_S3_REGION",
        "TRACKER_S3_SSE",
        "TRACKER_SAML_EMAIL_ATTRIBUTE",
        "TRACKER_SAML_IDP_ENTITY_ID",
        "TRACKER_SAML_IDP_METADATA_B64",
        "TRACKER_SAML_SP_CERTIFICATE_B64",
        "TRACKER_SAML_SP_PRIVATE_KEY_B64",
        "TRACKER_SCIM_BEARER_TOKEN",
        "TRACKER_SESSION_SECRET",
        "TRACKER_SLACK_API_URL",
        "TRACKER_SLACK_AUTHORIZE_URL",
        "TRACKER_SLACK_CLIENT_ID",
        "TRACKER_SLACK_CLIENT_SECRET",
        "TRACKER_SLACK_TOKEN_URL",
        "TRACKER_SMTP_FROM_EMAIL",
        "TRACKER_SMTP_HOST",
        "TRACKER_SMTP_PASSWORD",
        "TRACKER_SMTP_PORT",
        "TRACKER_SMTP_STARTTLS",
        "TRACKER_SMTP_TIMEOUT_SECONDS",
        "TRACKER_SMTP_USERNAME",
        "TRACKER_STORAGE_BACKEND",
        "TRACKER_WEB_REQUEST_LIMIT",
        "TRACKER_WISE_API_TOKEN",
        "TRACKER_WISE_API_URL",
        "TRACKER_WISE_BALANCE_ID",
        "TRACKER_WISE_PROFILE_ID",
        "TRACKER_WISE_SOURCE_CURRENCY",
        "TRACKER_WISE_WEBHOOK_PUBLIC_KEY_B64",
    }
)

PLACEHOLDER_PARTS = ("replace-with", "change-me", "<secret", "example-secret")
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


@dataclass(frozen=True, order=True)
class Finding:
    category: str
    variable: str
    problem: str


def _text(environment: Mapping[str, str], name: str) -> str:
    return str(environment.get(name, "")).strip()


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(part in lowered for part in PLACEHOLDER_PARTS)


def _require(
    findings: list[Finding],
    environment: Mapping[str, str],
    category: str,
    *names: str,
) -> bool:
    complete = True
    for name in names:
        value = _text(environment, name)
        if not value or _is_placeholder(value):
            findings.append(Finding(category, name, "is required"))
            complete = False
    return complete


def _problem(
    findings: list[Finding], category: str, variable: str, problem: str
) -> None:
    findings.append(Finding(category, variable, problem))


def _integer(
    findings: list[Finding],
    environment: Mapping[str, str],
    category: str,
    name: str,
    minimum: int,
    maximum: int | None = None,
    *,
    required: bool = False,
) -> int | None:
    value = _text(environment, name)
    if not value:
        if required:
            _problem(findings, category, name, "is required")
        return None
    try:
        parsed = int(value)
    except ValueError:
        _problem(findings, category, name, "must be an integer")
        return None
    if parsed < minimum or (maximum is not None and parsed > maximum):
        bound = f"between {minimum} and {maximum}" if maximum else f"at least {minimum}"
        _problem(findings, category, name, f"must be {bound}")
        return None
    return parsed


def _boolean(
    findings: list[Finding],
    environment: Mapping[str, str],
    category: str,
    name: str,
    *,
    required_true: bool = False,
) -> None:
    value = _text(environment, name).lower()
    if not value:
        if required_true:
            _problem(findings, category, name, "is required and must be true")
        return
    if value not in TRUE_VALUES | FALSE_VALUES:
        _problem(findings, category, name, "must be a boolean")
    elif required_true and value not in TRUE_VALUES:
        _problem(findings, category, name, "must be true")


def _secret(
    findings: list[Finding],
    environment: Mapping[str, str],
    category: str,
    name: str,
    minimum: int = 32,
) -> None:
    if not _require(findings, environment, category, name):
        return
    if len(_text(environment, name)) < minimum:
        _problem(
            findings, category, name, f"must contain at least {minimum} characters"
        )


def _decode_b64(value: str, *, urlsafe: bool = False) -> bytes | None:
    try:
        if urlsafe:
            return base64.b64decode(
                value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
            )
        return base64.b64decode(value, validate=True)
    except (binascii.Error, TypeError, ValueError):
        return None


def _base64_bytes(
    findings: list[Finding],
    environment: Mapping[str, str],
    category: str,
    name: str,
    size: int,
    *,
    urlsafe: bool = True,
) -> bytes | None:
    if not _require(findings, environment, category, name):
        return None
    decoded = _decode_b64(_text(environment, name), urlsafe=urlsafe)
    if decoded is None or len(decoded) != size:
        kind = "base64url" if urlsafe else "base64"
        _problem(
            findings, category, name, f"must be {kind} that decodes to {size} bytes"
        )
        return None
    return decoded


def _https_url(
    findings: list[Finding],
    environment: Mapping[str, str],
    category: str,
    name: str,
    *,
    required: bool = True,
    origin: bool = False,
) -> None:
    value = _text(environment, name)
    if not value:
        if required:
            _problem(findings, category, name, "is required")
        return
    parsed = urlparse(value)
    invalid = (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.fragment)
        or (origin and (parsed.path not in {"", "/"} or parsed.query))
    )
    if invalid:
        qualifier = " HTTPS origin" if origin else " valid HTTPS URL"
        _problem(findings, category, name, f"must be a{qualifier}")


def _check_core(environment: Mapping[str, str], findings: list[Finding]) -> None:
    category = "core"
    _require(
        findings,
        environment,
        category,
        "TRACKER_ENVIRONMENT",
        "TRACKER_PUBLIC_URL",
        "TRACKER_ALLOWED_HOSTS",
        "TRACKER_DATABASE_URL",
        "POSTGRES_PASSWORD",
        "TRACKER_ADMIN_EMAIL",
        "TRACKER_DATA_DIR",
    )
    deployment = _text(environment, "TRACKER_ENVIRONMENT").lower()
    if deployment and deployment != "production":
        _problem(findings, category, "TRACKER_ENVIRONMENT", "must be production")
    _https_url(findings, environment, category, "TRACKER_PUBLIC_URL", origin=True)
    _boolean(
        findings,
        environment,
        category,
        "TRACKER_COOKIE_SECURE",
        required_true=True,
    )
    hosts = [
        value.strip()
        for value in _text(environment, "TRACKER_ALLOWED_HOSTS").split(",")
        if value.strip()
    ]
    public_host = urlparse(_text(environment, "TRACKER_PUBLIC_URL")).hostname
    if hosts and ("*" in hosts or public_host not in hosts):
        _problem(
            findings,
            category,
            "TRACKER_ALLOWED_HOSTS",
            "must exclude wildcards and include the public URL hostname",
        )
    database = urlparse(_text(environment, "TRACKER_DATABASE_URL"))
    if _text(environment, "TRACKER_DATABASE_URL") and (
        database.scheme not in {"postgresql", "postgres"}
        or not database.hostname
        or not database.path.strip("/")
    ):
        _problem(
            findings,
            category,
            "TRACKER_DATABASE_URL",
            "must be a PostgreSQL URL containing a host and database name",
        )
    email = _text(environment, "TRACKER_ADMIN_EMAIL")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        _problem(
            findings, category, "TRACKER_ADMIN_EMAIL", "must be a valid email address"
        )
    _secret(findings, environment, category, "TRACKER_ADMIN_PASSWORD", 16)
    _secret(findings, environment, category, "TRACKER_SESSION_SECRET", 32)
    _base64_bytes(
        findings, environment, category, "TRACKER_DOCUMENT_ENCRYPTION_KEY", 32
    )
    if _require(findings, environment, category, "TRACKER_INTEGRATION_ENCRYPTION_KEYS"):
        try:
            CredentialKeyring.parse(
                _text(environment, "TRACKER_INTEGRATION_ENCRYPTION_KEYS")
            )
        except ValueError:
            _problem(
                findings,
                category,
                "TRACKER_INTEGRATION_ENCRYPTION_KEYS",
                "must be a valid credential keyring",
            )
    data_dir = _text(environment, "TRACKER_DATA_DIR")
    if data_dir and not Path(data_dir).is_absolute():
        _problem(findings, category, "TRACKER_DATA_DIR", "must be an absolute path")

    integer_bounds = {
        "TRACKER_MAX_UPLOAD_MB": (1, 1024),
        "TRACKER_RETENTION_DAYS": (1, 3650),
        "TRACKER_AUDIT_RETENTION_DAYS": (30, 3650),
        "TRACKER_DATABASE_MIN_POOL_SIZE": (1, 10_000),
        "TRACKER_DATABASE_MAX_POOL_SIZE": (1, 10_000),
        "TRACKER_INVITATION_HOURS": (1, 8760),
        "TRACKER_LOGIN_WINDOW_MINUTES": (1, 1440),
        "TRACKER_LOGIN_IDENTITY_FAILURE_LIMIT": (3, 100),
        "TRACKER_LOGIN_SOURCE_FAILURE_LIMIT": (10, 1000),
        "TRACKER_RATE_LIMIT_WINDOW_SECONDS": (1, 3600),
        "TRACKER_ANONYMOUS_REQUEST_LIMIT": (1, None),
        "TRACKER_WEB_REQUEST_LIMIT": (1, None),
        "TRACKER_DEVICE_REQUEST_LIMIT": (1, None),
        "TRACKER_DEVICE_REPLAY_REQUEST_LIMIT": (1, None),
    }
    parsed_integers: dict[str, int | None] = {}
    for name, (minimum, maximum) in integer_bounds.items():
        parsed_integers[name] = _integer(
            findings, environment, category, name, minimum, maximum
        )
    minimum_pool = parsed_integers["TRACKER_DATABASE_MIN_POOL_SIZE"]
    maximum_pool = parsed_integers["TRACKER_DATABASE_MAX_POOL_SIZE"]
    if (
        minimum_pool is not None
        and maximum_pool is not None
        and maximum_pool < minimum_pool
    ):
        _problem(
            findings,
            category,
            "TRACKER_DATABASE_MAX_POOL_SIZE",
            "must be at least TRACKER_DATABASE_MIN_POOL_SIZE",
        )
    log_level = _text(environment, "TRACKER_LOG_LEVEL")
    if log_level and log_level.upper() not in {
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
    }:
        _problem(
            findings,
            category,
            "TRACKER_LOG_LEVEL",
            "must be DEBUG, INFO, WARNING, ERROR, or CRITICAL",
        )


def _check_smtp(environment: Mapping[str, str], findings: list[Finding]) -> None:
    category = "smtp"
    _require(
        findings,
        environment,
        category,
        "TRACKER_SMTP_HOST",
        "TRACKER_SMTP_USERNAME",
        "TRACKER_SMTP_PASSWORD",
        "TRACKER_SMTP_FROM_EMAIL",
    )
    _integer(
        findings, environment, category, "TRACKER_SMTP_PORT", 1, 65535, required=True
    )
    _integer(
        findings,
        environment,
        category,
        "TRACKER_SMTP_TIMEOUT_SECONDS",
        1,
        60,
        required=True,
    )
    _boolean(
        findings,
        environment,
        category,
        "TRACKER_SMTP_STARTTLS",
        required_true=True,
    )
    from_email = _text(environment, "TRACKER_SMTP_FROM_EMAIL")
    if from_email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", from_email):
        _problem(
            findings,
            category,
            "TRACKER_SMTP_FROM_EMAIL",
            "must be a valid email address",
        )


def _check_s3(environment: Mapping[str, str], findings: list[Finding]) -> None:
    category = "s3"
    _require(
        findings,
        environment,
        category,
        "TRACKER_STORAGE_BACKEND",
        "TRACKER_S3_BUCKET",
        "TRACKER_S3_REGION",
        "TRACKER_S3_SSE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    )
    storage_backend = _text(environment, "TRACKER_STORAGE_BACKEND").lower()
    if storage_backend and storage_backend != "s3":
        _problem(findings, category, "TRACKER_STORAGE_BACKEND", "must be s3")
    bucket = _text(environment, "TRACKER_S3_BUCKET")
    if bucket and not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        _problem(
            findings, category, "TRACKER_S3_BUCKET", "must be a valid S3 bucket name"
        )
    sse = _text(environment, "TRACKER_S3_SSE")
    if sse and sse not in {"AES256", "aws:kms"}:
        _problem(findings, category, "TRACKER_S3_SSE", "must be AES256 or aws:kms")
    if sse == "aws:kms":
        _require(findings, environment, category, "TRACKER_S3_KMS_KEY_ID")
    _https_url(
        findings,
        environment,
        category,
        "TRACKER_S3_ENDPOINT_URL",
        required=False,
        origin=True,
    )


def _check_sso(environment: Mapping[str, str], findings: list[Finding]) -> None:
    category = "sso"
    _require(
        findings,
        environment,
        category,
        "TRACKER_OIDC_CLIENT_ID",
        "TRACKER_OIDC_CLIENT_SECRET",
        "TRACKER_SAML_IDP_ENTITY_ID",
        "TRACKER_SAML_IDP_METADATA_B64",
        "TRACKER_SAML_SP_PRIVATE_KEY_B64",
        "TRACKER_SAML_SP_CERTIFICATE_B64",
    )
    _https_url(findings, environment, category, "TRACKER_OIDC_ISSUER", origin=True)
    _https_url(
        findings,
        environment,
        category,
        "TRACKER_OIDC_DISCOVERY_URL",
        required=False,
    )
    _https_url(
        findings,
        environment,
        category,
        "TRACKER_OIDC_END_SESSION_URL",
        required=False,
    )
    auth_method = _text(environment, "TRACKER_OIDC_CLIENT_AUTH_METHOD")
    if auth_method and auth_method not in {"client_secret_basic", "client_secret_post"}:
        _problem(
            findings,
            category,
            "TRACKER_OIDC_CLIENT_AUTH_METHOD",
            "must be client_secret_basic or client_secret_post",
        )
    _secret(findings, environment, category, "TRACKER_SCIM_BEARER_TOKEN", 32)
    entity_id = _text(environment, "TRACKER_SAML_IDP_ENTITY_ID")
    if entity_id and (
        len(entity_id) > 1024 or any(char.isspace() for char in entity_id)
    ):
        _problem(
            findings,
            category,
            "TRACKER_SAML_IDP_ENTITY_ID",
            "has an invalid entity ID",
        )

    metadata_value = _text(environment, "TRACKER_SAML_IDP_METADATA_B64")
    metadata = _decode_b64(metadata_value) if metadata_value else None
    if metadata_value and (
        metadata is None
        or not metadata.strip().startswith(b"<")
        or b"<!DOCTYPE" in metadata.upper()
        or b"<!ENTITY" in metadata.upper()
    ):
        _problem(
            findings,
            category,
            "TRACKER_SAML_IDP_METADATA_B64",
            "must contain safe base64-encoded XML metadata",
        )
    key_value = _text(environment, "TRACKER_SAML_SP_PRIVATE_KEY_B64")
    certificate_value = _text(environment, "TRACKER_SAML_SP_CERTIFICATE_B64")
    key_data = _decode_b64(key_value) if key_value else None
    certificate_data = _decode_b64(certificate_value) if certificate_value else None
    private_key = None
    certificate = None
    if key_value:
        try:
            private_key = serialization.load_pem_private_key(
                key_data or b"", password=None
            )
            if (
                not isinstance(private_key, RSAPrivateKey)
                or private_key.key_size < 2048
            ):
                raise ValueError
        except (TypeError, ValueError):
            _problem(
                findings,
                category,
                "TRACKER_SAML_SP_PRIVATE_KEY_B64",
                "must contain a base64-encoded unencrypted RSA private key of at least 2048 bits",
            )
    if certificate_value:
        try:
            certificate = x509.load_pem_x509_certificate(certificate_data or b"")
        except ValueError:
            _problem(
                findings,
                category,
                "TRACKER_SAML_SP_CERTIFICATE_B64",
                "must contain a base64-encoded PEM certificate",
            )
    if (
        private_key is not None
        and certificate is not None
        and certificate.public_key().public_numbers()
        != private_key.public_key().public_numbers()
    ):
        _problem(
            findings,
            category,
            "TRACKER_SAML_SP_CERTIFICATE_B64",
            "must match TRACKER_SAML_SP_PRIVATE_KEY_B64",
        )
    email_attribute = _text(environment, "TRACKER_SAML_EMAIL_ATTRIBUTE")
    if email_attribute and not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9_.:-]{0,255}", email_attribute
    ):
        _problem(
            findings,
            category,
            "TRACKER_SAML_EMAIL_ATTRIBUTE",
            "has an invalid attribute name",
        )


def _check_signing(environment: Mapping[str, str], findings: list[Finding]) -> None:
    category = "signing"
    for name in (
        "TRACKER_AGENT_WINDOWS_URL",
        "TRACKER_AGENT_MACOS_URL",
        "TRACKER_AGENT_LINUX_URL",
        "TRACKER_AGENT_UPDATE_MANIFEST_URL",
        "WINDOWS_TIMESTAMP_URL",
    ):
        _https_url(findings, environment, category, name)
    public = _base64_bytes(
        findings, environment, category, "TRACKER_AGENT_UPDATE_PUBLIC_KEY", 32
    )
    seed = _base64_bytes(
        findings, environment, category, "DAYFINCH_UPDATE_SIGNING_KEY", 32
    )
    if public is not None and seed is not None:
        derived = (
            Ed25519PrivateKey.from_private_bytes(seed)
            .public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        )
        if derived != public:
            _problem(
                findings,
                category,
                "TRACKER_AGENT_UPDATE_PUBLIC_KEY",
                "must match DAYFINCH_UPDATE_SIGNING_KEY",
            )

    _require(
        findings,
        environment,
        category,
        "WINDOWS_CERTIFICATE_PFX",
        "WINDOWS_CERTIFICATE_PASSWORD",
        "MACOS_APPLICATION_CERTIFICATE_P12",
        "MACOS_INSTALLER_CERTIFICATE_P12",
        "MACOS_CERTIFICATE_PASSWORD",
        "MACOS_APPLICATION_IDENTITY",
        "MACOS_INSTALLER_IDENTITY",
        "MACOS_NOTARY_API_KEY_P8",
        "MACOS_NOTARY_KEY_ID",
        "MACOS_NOTARY_ISSUER",
    )
    windows_value = _text(environment, "WINDOWS_CERTIFICATE_PFX")
    windows_pfx = _decode_b64(windows_value) if windows_value else None
    if windows_value and windows_pfx is not None:
        try:
            key, certificate, _chain = pkcs12.load_key_and_certificates(
                windows_pfx,
                _text(environment, "WINDOWS_CERTIFICATE_PASSWORD").encode(),
            )
            if key is None or certificate is None:
                raise ValueError
        except (TypeError, ValueError):
            _problem(
                findings,
                category,
                "WINDOWS_CERTIFICATE_PFX",
                "must be a base64-encoded PFX readable with WINDOWS_CERTIFICATE_PASSWORD",
            )
    elif windows_value:
        _problem(
            findings,
            category,
            "WINDOWS_CERTIFICATE_PFX",
            "must be valid base64",
        )

    for name in (
        "MACOS_APPLICATION_CERTIFICATE_P12",
        "MACOS_INSTALLER_CERTIFICATE_P12",
    ):
        encoded = _text(environment, name)
        if not encoded:
            continue
        payload = _decode_b64(encoded)
        try:
            if payload is None:
                raise ValueError
            key, certificate, _chain = pkcs12.load_key_and_certificates(
                payload, _text(environment, "MACOS_CERTIFICATE_PASSWORD").encode()
            )
            if key is None or certificate is None:
                raise ValueError
        except (TypeError, ValueError):
            _problem(
                findings,
                category,
                name,
                "must be a base64-encoded PKCS#12 bundle readable with MACOS_CERTIFICATE_PASSWORD",
            )
    notary = _text(environment, "MACOS_NOTARY_API_KEY_P8")
    if notary:
        try:
            decoded = _decode_b64(notary)
            serialization.load_pem_private_key(decoded or b"", password=None)
        except (TypeError, ValueError):
            _problem(
                findings,
                category,
                "MACOS_NOTARY_API_KEY_P8",
                "must contain a base64-encoded unencrypted PEM private key",
            )
    key_id = _text(environment, "MACOS_NOTARY_KEY_ID")
    if key_id and not re.fullmatch(r"[A-Z0-9]{10}", key_id):
        _problem(
            findings,
            category,
            "MACOS_NOTARY_KEY_ID",
            "must be 10 uppercase letters/digits",
        )
    issuer = _text(environment, "MACOS_NOTARY_ISSUER")
    if issuer:
        try:
            uuid.UUID(issuer)
        except ValueError:
            _problem(findings, category, "MACOS_NOTARY_ISSUER", "must be a UUID")


def _rsa_private_key(
    findings: list[Finding], environment: Mapping[str, str], category: str, name: str
) -> None:
    value = _text(environment, name)
    if not value:
        _problem(findings, category, name, "is required")
        return
    try:
        key = serialization.load_pem_private_key(
            _decode_b64(value) or b"", password=None
        )
        if not isinstance(key, RSAPrivateKey) or key.key_size < 2048:
            raise ValueError
    except (TypeError, ValueError):
        _problem(
            findings,
            category,
            name,
            "must contain a base64-encoded unencrypted RSA private key of at least 2048 bits",
        )


def _check_integrations(
    environment: Mapping[str, str], findings: list[Finding]
) -> None:
    category = "integrations"
    _require(
        findings,
        environment,
        category,
        "TRACKER_GITHUB_APP_SLUG",
        "TRACKER_GITHUB_CLIENT_ID",
        "TRACKER_GITHUB_CLIENT_SECRET",
        "TRACKER_GITHUB_WEBHOOK_SECRET",
        "TRACKER_JIRA_CLIENT_ID",
        "TRACKER_JIRA_CLIENT_SECRET",
        "TRACKER_ASANA_CLIENT_ID",
        "TRACKER_ASANA_CLIENT_SECRET",
        "TRACKER_SLACK_CLIENT_ID",
        "TRACKER_SLACK_CLIENT_SECRET",
        "TRACKER_INTEGRATION_ENCRYPTION_KEYS",
        "TRACKER_PAYMENT_PROVIDER",
    )
    keyring = _text(environment, "TRACKER_INTEGRATION_ENCRYPTION_KEYS")
    if keyring:
        try:
            CredentialKeyring.parse(keyring)
        except ValueError:
            _problem(
                findings,
                category,
                "TRACKER_INTEGRATION_ENCRYPTION_KEYS",
                "must be a valid credential keyring",
            )
    slug = _text(environment, "TRACKER_GITHUB_APP_SLUG")
    if slug and not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?", slug):
        _problem(
            findings, category, "TRACKER_GITHUB_APP_SLUG", "has an invalid app slug"
        )
    _secret(findings, environment, category, "TRACKER_GITHUB_WEBHOOK_SECRET", 32)
    _rsa_private_key(findings, environment, category, "TRACKER_GITHUB_PRIVATE_KEY_B64")
    version = _text(environment, "TRACKER_GITHUB_API_VERSION")
    if version and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", version):
        _problem(findings, category, "TRACKER_GITHUB_API_VERSION", "must be YYYY-MM-DD")
    for name in (
        "TRACKER_GITHUB_API_URL",
        "TRACKER_GITHUB_WEB_URL",
        "TRACKER_JIRA_AUTHORIZE_URL",
        "TRACKER_JIRA_TOKEN_URL",
        "TRACKER_JIRA_API_URL",
        "TRACKER_ASANA_AUTHORIZE_URL",
        "TRACKER_ASANA_TOKEN_URL",
        "TRACKER_ASANA_TOKEN_INFO_URL",
        "TRACKER_ASANA_REVOKE_URL",
        "TRACKER_ASANA_API_URL",
        "TRACKER_SLACK_AUTHORIZE_URL",
        "TRACKER_SLACK_TOKEN_URL",
        "TRACKER_SLACK_API_URL",
    ):
        _https_url(findings, environment, category, name)

    provider = _text(environment, "TRACKER_PAYMENT_PROVIDER").lower()
    if provider and provider not in {"manual", "webhook", "paypal", "wise"}:
        _problem(
            findings,
            category,
            "TRACKER_PAYMENT_PROVIDER",
            "must be manual, webhook, paypal, or wise",
        )
    webhook_values = (
        _text(environment, "TRACKER_PAYMENT_WEBHOOK_URL"),
        _text(environment, "TRACKER_PAYMENT_WEBHOOK_SECRET"),
    )
    if any(webhook_values) or provider == "webhook":
        _https_url(findings, environment, category, "TRACKER_PAYMENT_WEBHOOK_URL")
        _secret(findings, environment, category, "TRACKER_PAYMENT_WEBHOOK_SECRET", 32)
    paypal_values = (
        _text(environment, "TRACKER_PAYPAL_CLIENT_ID"),
        _text(environment, "TRACKER_PAYPAL_CLIENT_SECRET"),
    )
    if any(paypal_values) or provider == "paypal":
        _require(
            findings,
            environment,
            category,
            "TRACKER_PAYPAL_CLIENT_ID",
            "TRACKER_PAYPAL_CLIENT_SECRET",
        )
        if _text(environment, "TRACKER_PAYPAL_API_URL") != "https://api-m.paypal.com":
            _problem(
                findings,
                category,
                "TRACKER_PAYPAL_API_URL",
                "must be the production PayPal API origin",
            )
    wise_values = (
        _text(environment, "TRACKER_WISE_API_TOKEN"),
        _text(environment, "TRACKER_WISE_PROFILE_ID"),
        _text(environment, "TRACKER_WISE_BALANCE_ID"),
    )
    if any(wise_values) or provider == "wise":
        _secret(findings, environment, category, "TRACKER_WISE_API_TOKEN", 20)
        _integer(
            findings, environment, category, "TRACKER_WISE_PROFILE_ID", 1, required=True
        )
        _integer(
            findings, environment, category, "TRACKER_WISE_BALANCE_ID", 1, required=True
        )
        if _text(environment, "TRACKER_WISE_API_URL") != "https://api.wise.com/2026Q3":
            _problem(
                findings,
                category,
                "TRACKER_WISE_API_URL",
                "must be the production Wise API base URL",
            )
        currency = _text(environment, "TRACKER_WISE_SOURCE_CURRENCY")
        if not re.fullmatch(r"[A-Za-z]{3}", currency):
            _problem(
                findings,
                category,
                "TRACKER_WISE_SOURCE_CURRENCY",
                "must be a three-letter currency code",
            )
    wise_webhook = _text(environment, "TRACKER_WISE_WEBHOOK_PUBLIC_KEY_B64")
    if wise_webhook:
        try:
            public_key = serialization.load_pem_public_key(
                _decode_b64(wise_webhook) or b""
            )
            if not isinstance(public_key, RSAPublicKey) or public_key.key_size < 2048:
                raise ValueError
        except (TypeError, ValueError):
            _problem(
                findings,
                category,
                "TRACKER_WISE_WEBHOOK_PUBLIC_KEY_B64",
                "must contain a base64-encoded RSA public key of at least 2048 bits",
            )


def _check_backups(environment: Mapping[str, str], findings: list[Finding]) -> None:
    category = "backups"
    _base64_bytes(findings, environment, category, "TRACKER_BACKUP_ENCRYPTION_KEY", 32)
    if _require(findings, environment, category, "DAYFINCH_BACKUP_DESTINATION"):
        destination = Path(_text(environment, "DAYFINCH_BACKUP_DESTINATION"))
        if not destination.is_absolute() or destination in {Path("/"), Path("/home")}:
            _problem(
                findings,
                category,
                "DAYFINCH_BACKUP_DESTINATION",
                "must be a scoped absolute path, not a filesystem root",
            )


def _check_monitoring(environment: Mapping[str, str], findings: list[Finding]) -> None:
    _secret(findings, environment, "monitoring", "TRACKER_METRICS_BEARER_TOKEN", 32)


CHECKS = {
    "core": _check_core,
    "smtp": _check_smtp,
    "s3": _check_s3,
    "sso": _check_sso,
    "signing": _check_signing,
    "integrations": _check_integrations,
    "backups": _check_backups,
    "monitoring": _check_monitoring,
}


def check_config(
    environment: Mapping[str, str], categories: Sequence[str] = CATEGORIES
) -> list[Finding]:
    findings: list[Finding] = []
    for category in categories:
        CHECKS[category](environment, findings)
    return sorted(set(findings))


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, original in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"line {line_number} is not a KEY=VALUE assignment")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[name] = value
    return values


def render(findings: Sequence[Finding], categories: Sequence[str]) -> str:
    lines = ["Dayfinch production configuration preflight"]
    for category in categories:
        category_findings = [item for item in findings if item.category == category]
        if category_findings:
            lines.append(f"[MISSING/INVALID] {category}")
            lines.extend(
                f"  - {item.variable}: {item.problem}" for item in category_findings
            )
        else:
            lines.append(f"[OK] {category}")
    lines.append(
        f"Result: {'NOT READY' if findings else 'READY'} "
        f"({len(findings)} missing/invalid setting{'s' if len(findings) != 1 else ''})"
    )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate Dayfinch production configuration without printing values "
            "or contacting external services"
        )
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="read KEY=VALUE assignments from this file (overrides process environment)",
    )
    parser.add_argument(
        "--category",
        action="append",
        choices=CATEGORIES,
        help="check one category; repeat for more (default: all)",
    )
    parser.add_argument("--json", action="store_true", help="emit value-free JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    import os

    environment = dict(os.environ)
    if arguments.env_file:
        try:
            environment.update(load_env_file(arguments.env_file))
        except (OSError, ValueError) as exc:
            print(f"check-config: {exc}", file=sys.stderr)
            return 2
    categories = tuple(arguments.category or CATEGORIES)
    findings = check_config(environment, categories)
    if arguments.json:
        print(
            json.dumps(
                {
                    "ready": not findings,
                    "categories": list(categories),
                    "findings": [asdict(item) for item in findings],
                },
                sort_keys=True,
            )
        )
    else:
        print(render(findings, categories))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import base64
import binascii
import ipaddress
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

WISE_PRODUCTION_WEBHOOK_PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAvO8vXV+JksBzZAY6GhSO
XdoTCfhXaaiZ+qAbtaDBiu2AGkGVpmEygFmWP4Li9m5+Ni85BhVvZOodM9epgW3F
bA5Q1SexvAF1PPjX4JpMstak/QhAgl1qMSqEevL8cmUeTgcMuVWCJmlge9h7B1CS
D4rtlimGZozG39rUBDg6Qt2K+P4wBfLblL0k4C4YUdLnpGYEDIth+i8XsRpFlogx
CAFyH9+knYsDbR43UJ9shtc42Ybd40Afihj8KnYKXzchyQ42aC8aZ/h5hyZ28yVy
Oj3Vos0VdBIs/gAyJ/4yyQFCXYte64I7ssrlbGRaco4nKF3HmaNhxwyKyJafz19e
HwIDAQAB
-----END PUBLIC KEY-----
"""
WISE_SANDBOX_WEBHOOK_PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAwpb91cEYuyJNQepZAVfP
ZIlPZfNUefH+n6w9SW3fykqKu938cR7WadQv87oF2VuT+fDt7kqeRziTmPSUhqPU
ys/V2Q1rlfJuXbE+Gga37t7zwd0egQ+KyOEHQOpcTwKmtZ81ieGHynAQzsn1We3j
wt760MsCPJ7GMT141ByQM+yW1Bx+4SG3IGjXWyqOWrcXsxAvIXkpUD/jK/L958Cg
nZEgz0BSEh0QxYLITnW1lLokSx/dTianWPFEhMC9BgijempgNXHNfcVirg1lPSyg
z7KqoKUN0oHqWLr2U1A+7kqrl6O2nx3CKs1bj1hToT1+p4kcMoHXA7kA+VBLUpEs
VwIDAQAB
-----END PUBLIC KEY-----
"""


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    admin_password: str
    session_secret: str
    cookie_secure: bool
    max_upload_bytes: int
    retention_days: int
    admin_email: str = "admin@example.local"
    audit_retention_days: int = 2555
    environment: str = "development"
    public_url: str = "http://127.0.0.1:8000"
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "testserver")
    database_url: str = "postgresql://dayfinch:dayfinch@127.0.0.1:5432/dayfinch"
    database_min_pool_size: int = 1
    database_max_pool_size: int = 10
    invitation_hours: int = 168
    login_window_minutes: int = 15
    login_identity_failure_limit: int = 8
    login_source_failure_limit: int = 50
    rate_limit_window_seconds: int = 60
    anonymous_request_limit: int = 120
    web_request_limit: int = 600
    device_request_limit: int = 600
    device_replay_request_limit: int = 6000
    storage_backend: str = "local"
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_endpoint_url: str = ""
    s3_sse: str = "AES256"
    s3_kms_key_id: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_starttls: bool = True
    smtp_timeout_seconds: int = 10
    oidc_issuer: str = ""
    oidc_discovery_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_client_auth_method: str = "client_secret_basic"
    oidc_end_session_url: str = ""
    saml_idp_entity_id: str = ""
    saml_idp_metadata_b64: str = ""
    saml_sp_private_key_b64: str = ""
    saml_sp_certificate_b64: str = ""
    saml_email_attribute: str = "email"
    scim_bearer_token: str = ""
    agent_windows_url: str = ""
    agent_macos_url: str = ""
    agent_linux_url: str = ""
    agent_update_manifest_url: str = ""
    agent_update_public_key: str = ""
    payment_provider: str = "webhook"
    payment_webhook_url: str = ""
    payment_webhook_secret: str = ""
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_api_url: str = "https://api-m.sandbox.paypal.com"
    wise_api_token: str = ""
    wise_profile_id: int = 0
    wise_balance_id: int = 0
    wise_source_currency: str = "USD"
    wise_api_url: str = "https://api.wise-sandbox.com/2026Q3"
    wise_webhook_public_key_b64: str = ""
    document_encryption_key: str = ""
    integration_encryption_keys: str = ""
    log_level: str = "INFO"
    metrics_bearer_token: str = ""
    github_app_slug: str = ""
    github_client_id: str = ""
    github_client_secret: str = ""
    github_private_key_b64: str = ""
    github_webhook_secret: str = ""
    github_api_url: str = "https://api.github.com"
    github_web_url: str = "https://github.com"
    github_api_version: str = "2026-03-10"
    jira_client_id: str = ""
    jira_client_secret: str = ""
    jira_authorize_url: str = "https://auth.atlassian.com/authorize"
    jira_token_url: str = "https://auth.atlassian.com/oauth/token"
    jira_api_url: str = "https://api.atlassian.com"
    asana_client_id: str = ""
    asana_client_secret: str = ""
    asana_authorize_url: str = "https://app.asana.com/-/oauth_authorize"
    asana_token_url: str = "https://app.asana.com/-/oauth_token"
    asana_token_info_url: str = "https://app.asana.com/-/token_info"
    asana_revoke_url: str = "https://app.asana.com/-/oauth_revoke"
    asana_api_url: str = "https://app.asana.com/api/1.0"
    slack_client_id: str = ""
    slack_client_secret: str = ""
    slack_authorize_url: str = "https://slack.com/oauth/v2/authorize"
    slack_token_url: str = "https://slack.com/api/oauth.v2.access"
    slack_api_url: str = "https://slack.com/api"

    @classmethod
    def from_env(cls) -> Settings:
        data_dir = Path(os.getenv("TRACKER_DATA_DIR", "runtime")).resolve()
        return cls(
            data_dir=data_dir,
            admin_password=os.getenv("TRACKER_ADMIN_PASSWORD", "change-me-before-use"),
            session_secret=os.getenv(
                "TRACKER_SESSION_SECRET",
                "local-development-secret-change-before-use",
            ),
            cookie_secure=_as_bool(os.getenv("TRACKER_COOKIE_SECURE", "false")),
            max_upload_bytes=int(os.getenv("TRACKER_MAX_UPLOAD_MB", "15"))
            * 1024
            * 1024,
            retention_days=max(1, int(os.getenv("TRACKER_RETENTION_DAYS", "30"))),
            audit_retention_days=min(
                max(30, int(os.getenv("TRACKER_AUDIT_RETENTION_DAYS", "2555"))),
                3650,
            ),
            admin_email=os.getenv("TRACKER_ADMIN_EMAIL", "admin@example.local")
            .strip()
            .lower(),
            environment=os.getenv("TRACKER_ENVIRONMENT", "development").strip().lower(),
            public_url=os.getenv("TRACKER_PUBLIC_URL", "http://127.0.0.1:8000")
            .strip()
            .rstrip("/"),
            allowed_hosts=tuple(
                host.strip()
                for host in os.getenv(
                    "TRACKER_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver"
                ).split(",")
                if host.strip()
            ),
            database_url=os.getenv(
                "TRACKER_DATABASE_URL",
                "postgresql://dayfinch:dayfinch@127.0.0.1:5432/dayfinch",
            ).strip(),
            database_min_pool_size=max(
                1, int(os.getenv("TRACKER_DATABASE_MIN_POOL_SIZE", "1"))
            ),
            database_max_pool_size=max(
                1, int(os.getenv("TRACKER_DATABASE_MAX_POOL_SIZE", "10"))
            ),
            invitation_hours=max(1, int(os.getenv("TRACKER_INVITATION_HOURS", "168"))),
            login_window_minutes=min(
                max(1, int(os.getenv("TRACKER_LOGIN_WINDOW_MINUTES", "15"))), 1440
            ),
            login_identity_failure_limit=min(
                max(3, int(os.getenv("TRACKER_LOGIN_IDENTITY_FAILURE_LIMIT", "8"))),
                100,
            ),
            login_source_failure_limit=min(
                max(10, int(os.getenv("TRACKER_LOGIN_SOURCE_FAILURE_LIMIT", "50"))),
                1000,
            ),
            rate_limit_window_seconds=min(
                max(1, int(os.getenv("TRACKER_RATE_LIMIT_WINDOW_SECONDS", "60"))),
                3600,
            ),
            anonymous_request_limit=max(
                1, int(os.getenv("TRACKER_ANONYMOUS_REQUEST_LIMIT", "120"))
            ),
            web_request_limit=max(
                1, int(os.getenv("TRACKER_WEB_REQUEST_LIMIT", "600"))
            ),
            device_request_limit=max(
                1, int(os.getenv("TRACKER_DEVICE_REQUEST_LIMIT", "600"))
            ),
            device_replay_request_limit=max(
                1, int(os.getenv("TRACKER_DEVICE_REPLAY_REQUEST_LIMIT", "6000"))
            ),
            storage_backend=os.getenv("TRACKER_STORAGE_BACKEND", "local")
            .strip()
            .lower(),
            s3_bucket=os.getenv("TRACKER_S3_BUCKET", "").strip(),
            s3_region=os.getenv("TRACKER_S3_REGION", "us-east-1").strip(),
            s3_endpoint_url=os.getenv("TRACKER_S3_ENDPOINT_URL", "").strip(),
            s3_sse=os.getenv("TRACKER_S3_SSE", "AES256").strip(),
            s3_kms_key_id=os.getenv("TRACKER_S3_KMS_KEY_ID", "").strip(),
            smtp_host=os.getenv("TRACKER_SMTP_HOST", "").strip(),
            smtp_port=int(os.getenv("TRACKER_SMTP_PORT", "587")),
            smtp_username=os.getenv("TRACKER_SMTP_USERNAME", "").strip(),
            smtp_password=os.getenv("TRACKER_SMTP_PASSWORD", ""),
            smtp_from_email=os.getenv("TRACKER_SMTP_FROM_EMAIL", "").strip(),
            smtp_starttls=_as_bool(os.getenv("TRACKER_SMTP_STARTTLS", "true")),
            smtp_timeout_seconds=min(
                max(1, int(os.getenv("TRACKER_SMTP_TIMEOUT_SECONDS", "10"))), 60
            ),
            oidc_issuer=os.getenv("TRACKER_OIDC_ISSUER", "").strip().rstrip("/"),
            oidc_discovery_url=os.getenv("TRACKER_OIDC_DISCOVERY_URL", "").strip(),
            oidc_client_id=os.getenv("TRACKER_OIDC_CLIENT_ID", "").strip(),
            oidc_client_secret=os.getenv("TRACKER_OIDC_CLIENT_SECRET", ""),
            oidc_client_auth_method=os.getenv(
                "TRACKER_OIDC_CLIENT_AUTH_METHOD", "client_secret_basic"
            ).strip(),
            oidc_end_session_url=os.getenv("TRACKER_OIDC_END_SESSION_URL", "").strip(),
            saml_idp_entity_id=os.getenv("TRACKER_SAML_IDP_ENTITY_ID", "").strip(),
            saml_idp_metadata_b64=os.getenv(
                "TRACKER_SAML_IDP_METADATA_B64", ""
            ).strip(),
            saml_sp_private_key_b64=os.getenv(
                "TRACKER_SAML_SP_PRIVATE_KEY_B64", ""
            ).strip(),
            saml_sp_certificate_b64=os.getenv(
                "TRACKER_SAML_SP_CERTIFICATE_B64", ""
            ).strip(),
            saml_email_attribute=os.getenv(
                "TRACKER_SAML_EMAIL_ATTRIBUTE", "email"
            ).strip(),
            scim_bearer_token=os.getenv("TRACKER_SCIM_BEARER_TOKEN", "").strip(),
            agent_windows_url=os.getenv("TRACKER_AGENT_WINDOWS_URL", "").strip(),
            agent_macos_url=os.getenv("TRACKER_AGENT_MACOS_URL", "").strip(),
            agent_linux_url=os.getenv("TRACKER_AGENT_LINUX_URL", "").strip(),
            agent_update_manifest_url=os.getenv(
                "TRACKER_AGENT_UPDATE_MANIFEST_URL", ""
            ).strip(),
            agent_update_public_key=os.getenv(
                "TRACKER_AGENT_UPDATE_PUBLIC_KEY", ""
            ).strip(),
            payment_provider=os.getenv("TRACKER_PAYMENT_PROVIDER", "webhook")
            .strip()
            .lower(),
            payment_webhook_url=os.getenv("TRACKER_PAYMENT_WEBHOOK_URL", "").strip(),
            payment_webhook_secret=os.getenv("TRACKER_PAYMENT_WEBHOOK_SECRET", ""),
            paypal_client_id=os.getenv("TRACKER_PAYPAL_CLIENT_ID", "").strip(),
            paypal_client_secret=os.getenv("TRACKER_PAYPAL_CLIENT_SECRET", ""),
            paypal_api_url=os.getenv(
                "TRACKER_PAYPAL_API_URL", "https://api-m.sandbox.paypal.com"
            )
            .strip()
            .rstrip("/"),
            wise_api_token=os.getenv("TRACKER_WISE_API_TOKEN", ""),
            wise_profile_id=int(os.getenv("TRACKER_WISE_PROFILE_ID", "0")),
            wise_balance_id=int(os.getenv("TRACKER_WISE_BALANCE_ID", "0")),
            wise_source_currency=os.getenv("TRACKER_WISE_SOURCE_CURRENCY", "USD")
            .strip()
            .upper(),
            wise_api_url=os.getenv(
                "TRACKER_WISE_API_URL", "https://api.wise-sandbox.com/2026Q3"
            )
            .strip()
            .rstrip("/"),
            wise_webhook_public_key_b64=os.getenv(
                "TRACKER_WISE_WEBHOOK_PUBLIC_KEY_B64", ""
            ).strip(),
            document_encryption_key=os.getenv(
                "TRACKER_DOCUMENT_ENCRYPTION_KEY", ""
            ).strip(),
            integration_encryption_keys=os.getenv(
                "TRACKER_INTEGRATION_ENCRYPTION_KEYS", ""
            ).strip(),
            log_level=os.getenv("TRACKER_LOG_LEVEL", "INFO").strip().upper(),
            metrics_bearer_token=os.getenv("TRACKER_METRICS_BEARER_TOKEN", "").strip(),
            github_app_slug=os.getenv("TRACKER_GITHUB_APP_SLUG", "").strip(),
            github_client_id=os.getenv("TRACKER_GITHUB_CLIENT_ID", "").strip(),
            github_client_secret=os.getenv("TRACKER_GITHUB_CLIENT_SECRET", ""),
            github_private_key_b64=os.getenv(
                "TRACKER_GITHUB_PRIVATE_KEY_B64", ""
            ).strip(),
            github_webhook_secret=os.getenv("TRACKER_GITHUB_WEBHOOK_SECRET", ""),
            github_api_url=os.getenv("TRACKER_GITHUB_API_URL", "https://api.github.com")
            .strip()
            .rstrip("/"),
            github_web_url=os.getenv("TRACKER_GITHUB_WEB_URL", "https://github.com")
            .strip()
            .rstrip("/"),
            github_api_version=os.getenv(
                "TRACKER_GITHUB_API_VERSION", "2026-03-10"
            ).strip(),
            jira_client_id=os.getenv("TRACKER_JIRA_CLIENT_ID", "").strip(),
            jira_client_secret=os.getenv("TRACKER_JIRA_CLIENT_SECRET", ""),
            jira_authorize_url=os.getenv(
                "TRACKER_JIRA_AUTHORIZE_URL", "https://auth.atlassian.com/authorize"
            ).strip(),
            jira_token_url=os.getenv(
                "TRACKER_JIRA_TOKEN_URL", "https://auth.atlassian.com/oauth/token"
            ).strip(),
            jira_api_url=os.getenv("TRACKER_JIRA_API_URL", "https://api.atlassian.com")
            .strip()
            .rstrip("/"),
            asana_client_id=os.getenv("TRACKER_ASANA_CLIENT_ID", "").strip(),
            asana_client_secret=os.getenv("TRACKER_ASANA_CLIENT_SECRET", ""),
            asana_authorize_url=os.getenv(
                "TRACKER_ASANA_AUTHORIZE_URL",
                "https://app.asana.com/-/oauth_authorize",
            ).strip(),
            asana_token_url=os.getenv(
                "TRACKER_ASANA_TOKEN_URL", "https://app.asana.com/-/oauth_token"
            ).strip(),
            asana_token_info_url=os.getenv(
                "TRACKER_ASANA_TOKEN_INFO_URL", "https://app.asana.com/-/token_info"
            ).strip(),
            asana_revoke_url=os.getenv(
                "TRACKER_ASANA_REVOKE_URL", "https://app.asana.com/-/oauth_revoke"
            ).strip(),
            asana_api_url=os.getenv(
                "TRACKER_ASANA_API_URL", "https://app.asana.com/api/1.0"
            )
            .strip()
            .rstrip("/"),
            slack_client_id=os.getenv("TRACKER_SLACK_CLIENT_ID", "").strip(),
            slack_client_secret=os.getenv("TRACKER_SLACK_CLIENT_SECRET", ""),
            slack_authorize_url=os.getenv(
                "TRACKER_SLACK_AUTHORIZE_URL",
                "https://slack.com/oauth/v2/authorize",
            ).strip(),
            slack_token_url=os.getenv(
                "TRACKER_SLACK_TOKEN_URL", "https://slack.com/api/oauth.v2.access"
            ).strip(),
            slack_api_url=os.getenv("TRACKER_SLACK_API_URL", "https://slack.com/api")
            .strip()
            .rstrip("/"),
        )

    @property
    def screenshot_dir(self) -> Path:
        return self.data_dir / "screenshots"

    def prepare(self) -> None:
        if not 30 <= self.audit_retention_days <= 3650:
            raise RuntimeError(
                "TRACKER_AUDIT_RETENTION_DAYS must be between 30 and 3650"
            )
        if self.environment not in {"development", "test", "production"}:
            raise RuntimeError(
                "TRACKER_ENVIRONMENT must be development, test, or production"
            )
        if not self.allowed_hosts:
            raise RuntimeError("TRACKER_ALLOWED_HOSTS must contain at least one host")
        public_url = urlparse(self.public_url)
        if (
            public_url.scheme not in {"http", "https"}
            or not public_url.netloc
            or public_url.path not in {"", "/"}
            or public_url.params
            or public_url.query
            or public_url.fragment
        ):
            raise RuntimeError(
                "TRACKER_PUBLIC_URL must be an http(s) origin without a path"
            )
        if not self.database_url.startswith(("postgresql://", "postgres://")):
            raise RuntimeError("TRACKER_DATABASE_URL must be a PostgreSQL URL")
        if self.database_max_pool_size < self.database_min_pool_size:
            raise RuntimeError(
                "TRACKER_DATABASE_MAX_POOL_SIZE must be at least the minimum"
            )
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise RuntimeError(
                "TRACKER_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL"
            )
        if self.metrics_bearer_token and len(self.metrics_bearer_token) < 32:
            raise RuntimeError(
                "TRACKER_METRICS_BEARER_TOKEN must contain at least 32 characters"
            )
        if self.integration_encryption_keys:
            # Keep parsing in the credential module so configuration validation and
            # runtime encryption cannot disagree about accepted key material.
            from .services.integration_credentials import CredentialKeyring

            try:
                CredentialKeyring.parse(self.integration_encryption_keys)
            except ValueError as exc:
                raise RuntimeError(
                    "TRACKER_INTEGRATION_ENCRYPTION_KEYS is invalid"
                ) from exc
        github_values = (
            self.github_app_slug,
            self.github_client_id,
            self.github_client_secret,
            self.github_private_key_b64,
            self.github_webhook_secret,
        )
        if any(github_values) and not all(github_values):
            raise RuntimeError(
                "TRACKER_GITHUB_APP_SLUG, TRACKER_GITHUB_CLIENT_ID, "
                "TRACKER_GITHUB_CLIENT_SECRET, TRACKER_GITHUB_PRIVATE_KEY_B64, and "
                "TRACKER_GITHUB_WEBHOOK_SECRET must be configured together"
            )
        if self.github_enabled:
            if not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?", self.github_app_slug
            ):
                raise RuntimeError("TRACKER_GITHUB_APP_SLUG is invalid")
            if len(self.github_webhook_secret) < 32:
                raise RuntimeError(
                    "TRACKER_GITHUB_WEBHOOK_SECRET must contain at least 32 characters"
                )
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", self.github_api_version):
                raise RuntimeError("TRACKER_GITHUB_API_VERSION must be YYYY-MM-DD")
            for label, value in (
                ("TRACKER_GITHUB_API_URL", self.github_api_url),
                ("TRACKER_GITHUB_WEB_URL", self.github_web_url),
            ):
                parsed_github = urlparse(value)
                if (
                    parsed_github.scheme not in {"http", "https"}
                    or not parsed_github.hostname
                    or parsed_github.username
                    or parsed_github.password
                    or parsed_github.query
                    or parsed_github.fragment
                ):
                    raise RuntimeError(f"{label} must be a valid HTTP(S) base URL")
            try:
                private_key_pem = base64.b64decode(
                    self.github_private_key_b64, validate=True
                )
                private_key = serialization.load_pem_private_key(
                    private_key_pem, password=None
                )
            except (TypeError, ValueError, binascii.Error) as exc:
                raise RuntimeError(
                    "TRACKER_GITHUB_PRIVATE_KEY_B64 must contain a base64-encoded PEM key"
                ) from exc
            if not isinstance(private_key, RSAPrivateKey):
                raise RuntimeError("The GitHub App private key must be RSA")
        jira_values = (self.jira_client_id, self.jira_client_secret)
        if any(jira_values) and not all(jira_values):
            raise RuntimeError(
                "TRACKER_JIRA_CLIENT_ID and TRACKER_JIRA_CLIENT_SECRET must be "
                "configured together"
            )
        if self.jira_enabled and not self.integration_encryption_keys:
            raise RuntimeError(
                "TRACKER_INTEGRATION_ENCRYPTION_KEYS is required for Jira OAuth"
            )
        if self.jira_enabled:
            for label, value in (
                ("TRACKER_JIRA_AUTHORIZE_URL", self.jira_authorize_url),
                ("TRACKER_JIRA_TOKEN_URL", self.jira_token_url),
                ("TRACKER_JIRA_API_URL", self.jira_api_url),
            ):
                parsed_jira = urlparse(value)
                if (
                    parsed_jira.scheme not in {"http", "https"}
                    or not parsed_jira.hostname
                    or parsed_jira.username
                    or parsed_jira.password
                    or parsed_jira.fragment
                    or (label != "TRACKER_JIRA_AUTHORIZE_URL" and parsed_jira.query)
                ):
                    raise RuntimeError(f"{label} must be a valid HTTP(S) URL")
        asana_values = (self.asana_client_id, self.asana_client_secret)
        if any(asana_values) and not all(asana_values):
            raise RuntimeError(
                "TRACKER_ASANA_CLIENT_ID and TRACKER_ASANA_CLIENT_SECRET must be "
                "configured together"
            )
        if self.asana_enabled and not self.integration_encryption_keys:
            raise RuntimeError(
                "TRACKER_INTEGRATION_ENCRYPTION_KEYS is required for Asana OAuth"
            )
        if self.asana_enabled:
            for label, value in (
                ("TRACKER_ASANA_AUTHORIZE_URL", self.asana_authorize_url),
                ("TRACKER_ASANA_TOKEN_URL", self.asana_token_url),
                ("TRACKER_ASANA_TOKEN_INFO_URL", self.asana_token_info_url),
                ("TRACKER_ASANA_REVOKE_URL", self.asana_revoke_url),
                ("TRACKER_ASANA_API_URL", self.asana_api_url),
            ):
                parsed_asana = urlparse(value)
                if (
                    parsed_asana.scheme not in {"http", "https"}
                    or not parsed_asana.hostname
                    or parsed_asana.username
                    or parsed_asana.password
                    or parsed_asana.query
                    or parsed_asana.fragment
                ):
                    raise RuntimeError(f"{label} must be a valid HTTP(S) URL")
        slack_values = (self.slack_client_id, self.slack_client_secret)
        if any(slack_values) and not all(slack_values):
            raise RuntimeError(
                "TRACKER_SLACK_CLIENT_ID and TRACKER_SLACK_CLIENT_SECRET must be "
                "configured together"
            )
        if self.slack_enabled and not self.integration_encryption_keys:
            raise RuntimeError(
                "TRACKER_INTEGRATION_ENCRYPTION_KEYS is required for Slack OAuth"
            )
        if self.slack_enabled:
            for label, value in (
                ("TRACKER_SLACK_AUTHORIZE_URL", self.slack_authorize_url),
                ("TRACKER_SLACK_TOKEN_URL", self.slack_token_url),
                ("TRACKER_SLACK_API_URL", self.slack_api_url),
            ):
                parsed_slack = urlparse(value)
                if (
                    parsed_slack.scheme not in {"http", "https"}
                    or not parsed_slack.hostname
                    or parsed_slack.username
                    or parsed_slack.password
                    or parsed_slack.query
                    or parsed_slack.fragment
                ):
                    raise RuntimeError(f"{label} must be a valid HTTP(S) URL")
        oidc_values = (self.oidc_issuer, self.oidc_client_id, self.oidc_client_secret)
        if any(oidc_values) and not all(oidc_values):
            raise RuntimeError(
                "TRACKER_OIDC_ISSUER, TRACKER_OIDC_CLIENT_ID, and "
                "TRACKER_OIDC_CLIENT_SECRET must be configured together"
            )
        if self.oidc_enabled:
            for label, value in (
                ("TRACKER_OIDC_ISSUER", self.oidc_issuer),
                ("TRACKER_OIDC_DISCOVERY_URL", self.oidc_metadata_url),
            ):
                parsed_oidc = urlparse(value)
                if (
                    parsed_oidc.scheme not in {"http", "https"}
                    or not parsed_oidc.hostname
                    or parsed_oidc.query
                    or parsed_oidc.fragment
                ):
                    raise RuntimeError(f"{label} must be a valid HTTP(S) URL")
            if self.oidc_client_auth_method not in {
                "client_secret_basic",
                "client_secret_post",
            }:
                raise RuntimeError(
                    "TRACKER_OIDC_CLIENT_AUTH_METHOD must be client_secret_basic "
                    "or client_secret_post"
                )
        if self.oidc_end_session_url:
            parsed_logout = urlparse(self.oidc_end_session_url)
            if (
                not self.oidc_enabled
                or parsed_logout.scheme not in {"http", "https"}
                or not parsed_logout.hostname
                or parsed_logout.username
                or parsed_logout.password
                or parsed_logout.query
                or parsed_logout.fragment
            ):
                raise RuntimeError(
                    "TRACKER_OIDC_END_SESSION_URL must be an HTTP(S) endpoint and "
                    "requires complete OIDC configuration"
                )
        saml_values = (
            self.saml_idp_entity_id,
            self.saml_idp_metadata_b64,
            self.saml_sp_private_key_b64,
            self.saml_sp_certificate_b64,
        )
        if any(saml_values) and not all(saml_values):
            raise RuntimeError(
                "TRACKER_SAML_IDP_ENTITY_ID, TRACKER_SAML_IDP_METADATA_B64, "
                "TRACKER_SAML_SP_PRIVATE_KEY_B64, and "
                "TRACKER_SAML_SP_CERTIFICATE_B64 must be configured together"
            )
        if self.saml_enabled:
            if len(self.saml_idp_entity_id) > 1024 or any(
                character.isspace() for character in self.saml_idp_entity_id
            ):
                raise RuntimeError("TRACKER_SAML_IDP_ENTITY_ID is invalid")
            if not re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_.:-]{0,255}", self.saml_email_attribute
            ):
                raise RuntimeError("TRACKER_SAML_EMAIL_ATTRIBUTE is invalid")
            metadata = self._saml_b64(
                "TRACKER_SAML_IDP_METADATA_B64",
                self.saml_idp_metadata_b64,
                1_048_576,
            )
            private_key_data = self._saml_b64(
                "TRACKER_SAML_SP_PRIVATE_KEY_B64",
                self.saml_sp_private_key_b64,
                64 * 1024,
            )
            certificate_data = self._saml_b64(
                "TRACKER_SAML_SP_CERTIFICATE_B64",
                self.saml_sp_certificate_b64,
                64 * 1024,
            )
            if b"<!DOCTYPE" in metadata.upper() or b"<!ENTITY" in metadata.upper():
                raise RuntimeError("SAML metadata cannot contain a DTD or entities")
            try:
                private_key = serialization.load_pem_private_key(
                    private_key_data, password=None
                )
                certificate = x509.load_pem_x509_certificate(certificate_data)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "SAML SP certificate or private key is invalid"
                ) from exc
            certificate_key = certificate.public_key()
            if (
                not isinstance(private_key, RSAPrivateKey)
                or private_key.key_size < 2048
                or not isinstance(certificate_key, RSAPublicKey)
                or certificate_key.key_size < 2048
                or certificate_key.public_numbers()
                != private_key.public_key().public_numbers()
            ):
                raise RuntimeError(
                    "SAML SP certificate and RSA private key do not form a secure pair"
                )
            now = datetime.now(UTC)
            if not (
                certificate.not_valid_before_utc
                <= now
                < certificate.not_valid_after_utc
            ):
                raise RuntimeError("SAML SP certificate is not currently valid")
        if self.scim_bearer_token and len(self.scim_bearer_token) < 32:
            raise RuntimeError(
                "TRACKER_SCIM_BEARER_TOKEN must contain at least 32 characters"
            )
        for label, value in (
            ("TRACKER_AGENT_WINDOWS_URL", self.agent_windows_url),
            ("TRACKER_AGENT_MACOS_URL", self.agent_macos_url),
            ("TRACKER_AGENT_LINUX_URL", self.agent_linux_url),
        ):
            if not value:
                continue
            parsed_download = urlparse(value)
            if (
                parsed_download.scheme not in {"http", "https"}
                or not parsed_download.hostname
                or parsed_download.username
                or parsed_download.password
                or parsed_download.fragment
            ):
                raise RuntimeError(f"{label} must be a valid HTTP(S) URL")
        if bool(self.agent_update_manifest_url) != bool(self.agent_update_public_key):
            raise RuntimeError(
                "TRACKER_AGENT_UPDATE_MANIFEST_URL and "
                "TRACKER_AGENT_UPDATE_PUBLIC_KEY must be configured together"
            )
        if self.agent_update_manifest_url:
            parsed_manifest = urlparse(self.agent_update_manifest_url)
            if (
                parsed_manifest.scheme not in {"http", "https"}
                or not parsed_manifest.hostname
                or parsed_manifest.username
                or parsed_manifest.password
                or parsed_manifest.fragment
            ):
                raise RuntimeError(
                    "TRACKER_AGENT_UPDATE_MANIFEST_URL must be a valid HTTP(S) URL"
                )
            if parsed_manifest.scheme != "https" and not _is_loopback(
                parsed_manifest.hostname
            ):
                raise RuntimeError(
                    "TRACKER_AGENT_UPDATE_MANIFEST_URL requires HTTPS unless hosted "
                    "on localhost"
                )
            try:
                padding = "=" * (-len(self.agent_update_public_key) % 4)
                public_key = base64.b64decode(
                    self.agent_update_public_key + padding,
                    altchars=b"-_",
                    validate=True,
                )
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "TRACKER_AGENT_UPDATE_PUBLIC_KEY must be valid base64url"
                ) from exc
            if len(public_key) != 32:
                raise RuntimeError(
                    "TRACKER_AGENT_UPDATE_PUBLIC_KEY must decode to 32 bytes"
                )
        if self.payment_provider not in {"manual", "webhook", "paypal", "wise"}:
            raise RuntimeError(
                "TRACKER_PAYMENT_PROVIDER must be manual, webhook, paypal, or wise"
            )
        webhook_values = (self.payment_webhook_url, self.payment_webhook_secret)
        if any(webhook_values) and not all(webhook_values):
            raise RuntimeError(
                "TRACKER_PAYMENT_WEBHOOK_URL and TRACKER_PAYMENT_WEBHOOK_SECRET "
                "must be configured together"
            )
        if self.payment_provider == "webhook" and all(webhook_values):
            parsed_webhook = urlparse(self.payment_webhook_url)
            if (
                parsed_webhook.scheme not in {"http", "https"}
                or not parsed_webhook.hostname
                or parsed_webhook.username
                or parsed_webhook.password
                or parsed_webhook.fragment
            ):
                raise RuntimeError(
                    "TRACKER_PAYMENT_WEBHOOK_URL must be a valid HTTP(S) endpoint"
                )
            if len(self.payment_webhook_secret) < 32:
                raise RuntimeError(
                    "TRACKER_PAYMENT_WEBHOOK_SECRET must contain at least 32 characters"
                )
        paypal_values = (self.paypal_client_id, self.paypal_client_secret)
        if any(paypal_values) and not all(paypal_values):
            raise RuntimeError(
                "TRACKER_PAYPAL_CLIENT_ID and TRACKER_PAYPAL_CLIENT_SECRET must be "
                "configured together"
            )
        parsed_paypal = urlparse(self.paypal_api_url)
        if (
            parsed_paypal.scheme not in {"http", "https"}
            or not parsed_paypal.hostname
            or parsed_paypal.path not in {"", "/"}
            or parsed_paypal.username
            or parsed_paypal.password
            or parsed_paypal.query
            or parsed_paypal.fragment
        ):
            raise RuntimeError("TRACKER_PAYPAL_API_URL must be an HTTP(S) origin")
        if self.payment_provider == "paypal" and not all(paypal_values):
            raise RuntimeError(
                "PayPal payroll requires TRACKER_PAYPAL_CLIENT_ID and "
                "TRACKER_PAYPAL_CLIENT_SECRET"
            )
        parsed_wise = urlparse(self.wise_api_url)
        if (
            parsed_wise.scheme not in {"http", "https"}
            or not parsed_wise.hostname
            or parsed_wise.path in {"", "/"}
            or parsed_wise.username
            or parsed_wise.password
            or parsed_wise.query
            or parsed_wise.fragment
        ):
            raise RuntimeError(
                "TRACKER_WISE_API_URL must be an HTTP(S) versioned API base URL"
            )
        if self.payment_provider == "wise":
            if (
                len(self.wise_api_token) < 20
                or self.wise_profile_id <= 0
                or self.wise_balance_id <= 0
            ):
                raise RuntimeError(
                    "Wise payroll requires a token, positive profile ID, and positive "
                    "balance ID"
                )
            if (
                len(self.wise_source_currency) != 3
                or not self.wise_source_currency.isalpha()
            ):
                raise RuntimeError(
                    "TRACKER_WISE_SOURCE_CURRENCY must be a three-letter code"
                )
        if self.wise_webhook_public_key_b64:
            try:
                decoded = base64.b64decode(
                    self.wise_webhook_public_key_b64, validate=True
                )
                public_key = serialization.load_pem_public_key(decoded)
            except (binascii.Error, TypeError, UnsupportedAlgorithm, ValueError) as exc:
                raise RuntimeError(
                    "TRACKER_WISE_WEBHOOK_PUBLIC_KEY_B64 must contain a base64-encoded PEM public key"
                ) from exc
            if not isinstance(public_key, RSAPublicKey) or public_key.key_size < 2048:
                raise RuntimeError(
                    "TRACKER_WISE_WEBHOOK_PUBLIC_KEY_B64 must contain an RSA key of at least 2048 bits"
                )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.storage_backend == "local":
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        elif self.storage_backend == "s3":
            if not self.s3_bucket:
                raise RuntimeError("TRACKER_S3_BUCKET is required for S3 storage")
        else:
            raise RuntimeError("TRACKER_STORAGE_BACKEND must be 'local' or 's3'")

    def validate_for_nonlocal(self) -> None:
        if self.admin_password == "change-me-before-use":
            raise RuntimeError("Set TRACKER_ADMIN_PASSWORD before non-local deployment")
        if self.session_secret == "local-development-secret-change-before-use":
            raise RuntimeError("Set TRACKER_SESSION_SECRET before non-local deployment")
        if self.admin_email == "admin@example.local":
            raise RuntimeError("Set TRACKER_ADMIN_EMAIL before non-local deployment")
        if not self.public_url.startswith("https://"):
            raise RuntimeError(
                "TRACKER_PUBLIC_URL must use HTTPS outside local development"
            )
        if not self.cookie_secure:
            raise RuntimeError("TRACKER_COOKIE_SECURE must be true in production")
        if "*" in self.allowed_hosts:
            raise RuntimeError("TRACKER_ALLOWED_HOSTS cannot contain '*' in production")
        public_host = urlparse(self.public_url).hostname
        if public_host not in self.allowed_hosts:
            raise RuntimeError(
                "TRACKER_ALLOWED_HOSTS must include the TRACKER_PUBLIC_URL hostname"
            )
        if self.oidc_enabled and not (
            self.oidc_issuer.startswith("https://")
            and self.oidc_metadata_url.startswith("https://")
        ):
            raise RuntimeError("OIDC issuer and discovery URLs must use HTTPS")
        if self.oidc_end_session_url and not self.oidc_end_session_url.startswith(
            "https://"
        ):
            raise RuntimeError("OIDC end-session URL must use HTTPS")
        if self.github_enabled and (
            not self.github_api_url.startswith("https://")
            or not self.github_web_url.startswith("https://")
        ):
            raise RuntimeError("GitHub base URLs must use HTTPS in production")
        if self.jira_enabled and any(
            not value.startswith("https://")
            for value in (
                self.jira_authorize_url,
                self.jira_token_url,
                self.jira_api_url,
            )
        ):
            raise RuntimeError("Jira OAuth and API URLs must use HTTPS in production")
        if self.asana_enabled and any(
            not value.startswith("https://")
            for value in (
                self.asana_authorize_url,
                self.asana_token_url,
                self.asana_token_info_url,
                self.asana_revoke_url,
                self.asana_api_url,
            )
        ):
            raise RuntimeError("Asana OAuth and API URLs must use HTTPS in production")
        if self.slack_enabled and any(
            not value.startswith("https://")
            for value in (
                self.slack_authorize_url,
                self.slack_token_url,
                self.slack_api_url,
            )
        ):
            raise RuntimeError("Slack OAuth and API URLs must use HTTPS in production")
        if any(
            url and not url.startswith("https://")
            for url in (
                self.agent_windows_url,
                self.agent_macos_url,
                self.agent_linux_url,
            )
        ):
            raise RuntimeError("Desktop agent download URLs must use HTTPS")
        if (
            self.agent_update_manifest_url
            and not self.agent_update_manifest_url.startswith("https://")
        ):
            raise RuntimeError("Desktop agent update manifest URL must use HTTPS")
        if (
            self.payment_provider == "webhook"
            and self.payment_webhook_url
            and not self.payment_webhook_url.startswith("https://")
        ):
            raise RuntimeError("Payroll webhook URL must use HTTPS in production")
        if (
            self.payment_provider == "paypal"
            and self.paypal_api_url != "https://api-m.paypal.com"
        ):
            raise RuntimeError(
                "Production PayPal payroll must use https://api-m.paypal.com"
            )
        if (
            self.payment_provider == "wise"
            and self.wise_api_url != "https://api.wise.com/2026Q3"
        ):
            raise RuntimeError(
                "Production Wise payroll must use https://api.wise.com/2026Q3"
            )

    @property
    def oidc_enabled(self) -> bool:
        return bool(
            self.oidc_issuer and self.oidc_client_id and self.oidc_client_secret
        )

    @property
    def oidc_metadata_url(self) -> str:
        return self.oidc_discovery_url or (
            f"{self.oidc_issuer}/.well-known/openid-configuration"
            if self.oidc_issuer
            else ""
        )

    @property
    def saml_enabled(self) -> bool:
        return bool(
            self.saml_idp_entity_id
            and self.saml_idp_metadata_b64
            and self.saml_sp_private_key_b64
            and self.saml_sp_certificate_b64
        )

    @property
    def saml_sp_entity_id(self) -> str:
        return f"{self.public_url}/auth/saml/metadata"

    @staticmethod
    def _saml_b64(label: str, value: str, maximum: int) -> bytes:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RuntimeError(f"{label} must be valid base64") from exc
        if not decoded or len(decoded) > maximum:
            raise RuntimeError(f"{label} has an invalid decoded size")
        return decoded

    @property
    def github_enabled(self) -> bool:
        return bool(
            self.github_app_slug
            and self.github_client_id
            and self.github_client_secret
            and self.github_private_key_b64
            and self.github_webhook_secret
        )

    @property
    def jira_enabled(self) -> bool:
        return bool(self.jira_client_id and self.jira_client_secret)

    @property
    def asana_enabled(self) -> bool:
        return bool(self.asana_client_id and self.asana_client_secret)

    @property
    def slack_enabled(self) -> bool:
        return bool(self.slack_client_id and self.slack_client_secret)

    @property
    def wise_webhook_public_key_pem(self) -> bytes:
        if self.wise_webhook_public_key_b64:
            return base64.b64decode(self.wise_webhook_public_key_b64, validate=True)
        if self.wise_api_url == "https://api.wise.com/2026Q3":
            return WISE_PRODUCTION_WEBHOOK_PUBLIC_KEY
        return WISE_SANDBOX_WEBHOOK_PUBLIC_KEY

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    admin_password: str
    session_secret: str
    cookie_secure: bool
    max_upload_bytes: int
    retention_days: int
    admin_email: str = "admin@example.local"
    database_url: str = "postgresql://dayfinch:dayfinch@127.0.0.1:5432/dayfinch"
    database_min_pool_size: int = 1
    database_max_pool_size: int = 10
    invitation_hours: int = 168
    storage_backend: str = "local"
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_endpoint_url: str = ""
    s3_sse: str = "AES256"
    s3_kms_key_id: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
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
            admin_email=os.getenv("TRACKER_ADMIN_EMAIL", "admin@example.local")
            .strip()
            .lower(),
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
            storage_backend=os.getenv("TRACKER_STORAGE_BACKEND", "local").strip().lower(),
            s3_bucket=os.getenv("TRACKER_S3_BUCKET", "").strip(),
            s3_region=os.getenv("TRACKER_S3_REGION", "us-east-1").strip(),
            s3_endpoint_url=os.getenv("TRACKER_S3_ENDPOINT_URL", "").strip(),
            s3_sse=os.getenv("TRACKER_S3_SSE", "AES256").strip(),
            s3_kms_key_id=os.getenv("TRACKER_S3_KMS_KEY_ID", "").strip(),
        )

    @property
    def screenshot_dir(self) -> Path:
        return self.data_dir / "screenshots"

    def prepare(self) -> None:
        if not self.database_url.startswith(("postgresql://", "postgres://")):
            raise RuntimeError("TRACKER_DATABASE_URL must be a PostgreSQL URL")
        if self.database_max_pool_size < self.database_min_pool_size:
            raise RuntimeError(
                "TRACKER_DATABASE_MAX_POOL_SIZE must be at least the minimum"
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

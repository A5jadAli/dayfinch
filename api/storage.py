from __future__ import annotations

import io
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Protocol

from .config import Settings

ALLOWED_SIGNATURES = {
    b"\xff\xd8\xff": (".jpg", "image/jpeg"),
    b"\x89PNG\r\n\x1a\n": (".png", "image/png"),
}


@dataclass(frozen=True)
class StoredScreenshot:
    key: str
    version_id: str | None = None


@dataclass(frozen=True)
class ScreenshotContent:
    data: bytes
    content_type: str


class ScreenshotStore(Protocol):
    def save(
        self, device_id: str, record_id: str, captured_at: datetime, data: bytes
    ) -> StoredScreenshot: ...

    def read(self, key: str) -> ScreenshotContent: ...

    def delete(self, key: str, version_id: str | None = None) -> None: ...


def image_type(data: bytes) -> tuple[str, str]:
    for signature, result in ALLOWED_SIGNATURES.items():
        if data.startswith(signature):
            return result
    raise ValueError("Screenshot must be a JPEG or PNG image")


def object_key(
    device_id: str, record_id: str, captured_at: datetime, extension: str
) -> str:
    parts = [
        part
        for part in (
            device_id,
            captured_at.strftime("%Y/%m/%d"),
            record_id + extension,
        )
        if part
    ]
    return PurePosixPath(*parts).as_posix()


class LocalScreenshotStorage:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def extension_for(data: bytes) -> str:
        return image_type(data)[0]

    def save(
        self, device_id: str, record_id: str, captured_at: datetime, data: bytes
    ) -> StoredScreenshot:
        extension, _ = image_type(data)
        relative = object_key(device_id, record_id, captured_at, extension)
        destination = self._resolve(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        return StoredScreenshot(relative)

    def _resolve(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise ValueError("Invalid screenshot path")
        return path

    # Kept as a public helper for local diagnostics and backwards-compatible tests.
    def resolve(self, key: str) -> Path:
        return self._resolve(key)

    def read(self, key: str) -> ScreenshotContent:
        path = self._resolve(key)
        data = path.read_bytes()
        _, content_type = image_type(data)
        return ScreenshotContent(data, content_type)

    def delete(self, key: str, version_id: str | None = None) -> None:
        del version_id
        self._resolve(key).unlink(missing_ok=True)


class S3ScreenshotStorage:
    def __init__(self, settings: Settings):
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError(
                'Install S3 support with: pip install -e ".[s3]"'
            ) from exc
        self.bucket = settings.s3_bucket
        self.sse = settings.s3_sse
        self.kms_key_id = settings.s3_kms_key_id
        self.client = boto3.client(
            "s3",
            region_name=settings.s3_region or None,
            endpoint_url=settings.s3_endpoint_url or None,
        )

    def save(
        self, device_id: str, record_id: str, captured_at: datetime, data: bytes
    ) -> StoredScreenshot:
        extension, content_type = image_type(data)
        key = object_key(device_id, record_id, captured_at, extension)
        arguments: dict[str, object] = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": io.BytesIO(data),
            "ContentType": content_type,
            "CacheControl": "private, no-store",
        }
        if self.sse:
            arguments["ServerSideEncryption"] = self.sse
        if self.sse == "aws:kms" and self.kms_key_id:
            arguments["SSEKMSKeyId"] = self.kms_key_id
        response = self.client.put_object(**arguments)
        return StoredScreenshot(key, response.get("VersionId"))

    def read(self, key: str) -> ScreenshotContent:
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        return ScreenshotContent(
            response["Body"].read(),
            response.get("ContentType") or "application/octet-stream",
        )

    def delete(self, key: str, version_id: str | None = None) -> None:
        arguments = {"Bucket": self.bucket, "Key": key}
        if version_id:
            arguments["VersionId"] = version_id
        self.client.delete_object(**arguments)


def create_storage(settings: Settings) -> ScreenshotStore:
    if settings.storage_backend == "s3":
        return S3ScreenshotStorage(settings)
    return LocalScreenshotStorage(settings.screenshot_dir)


# Previous public name remains available for local-storage callers.
ScreenshotStorage = LocalScreenshotStorage

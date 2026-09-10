from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MAX_MANIFEST_BYTES = 128 * 1024
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_REDIRECTS = 3
_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class UpdateError(ValueError):
    """The update channel returned unsafe, invalid, or corrupt data."""


@dataclass(frozen=True)
class UpdateArtifact:
    platform: str
    filename: str
    url: str
    sha256: str
    size: int


@dataclass(frozen=True)
class AvailableUpdate:
    version: str
    published_at: str
    artifact: UpdateArtifact


def _decode_base64url(value: str, expected_bytes: int, label: str) -> bytes:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise UpdateError(f"{label} must be valid base64url") from exc
    if len(decoded) != expected_bytes:
        raise UpdateError(f"{label} must decode to {expected_bytes} bytes")
    return decoded


def decode_public_key(value: str) -> bytes:
    return _decode_base64url(value.strip(), 32, "update public key")


def canonical_manifest(payload: dict[str, Any]) -> bytes:
    signed = dict(payload)
    signed.pop("signature", None)
    return json.dumps(
        signed, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def platform_key(system: str | None = None, machine: str | None = None) -> str:
    system_name = (system or platform.system()).strip().lower()
    machine_name = (machine or platform.machine()).strip().lower()
    operating_system = {
        "linux": "linux",
        "windows": "windows",
        "darwin": "macos",
    }.get(system_name)
    architecture = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine_name)
    if not operating_system or not architecture:
        raise UpdateError(
            f"No desktop release is defined for {system_name or 'unknown'} "
            f"{machine_name or 'unknown'}"
        )
    return f"{operating_system}-{architecture}"


def _semver(
    value: str,
) -> tuple[int, int, int, tuple[tuple[int, int | str], ...] | None]:
    matched = _SEMVER.fullmatch(value)
    if not matched:
        raise UpdateError(f"Invalid semantic version: {value}")
    prerelease = matched.group(4)
    identifiers = None
    if prerelease is not None:
        identifiers = tuple(
            (0, int(item)) if item.isdigit() else (1, item)
            for item in prerelease.split(".")
        )
    return (
        int(matched.group(1)),
        int(matched.group(2)),
        int(matched.group(3)),
        identifiers,
    )


def version_is_newer(candidate: str, current: str) -> bool:
    candidate_version = _semver(candidate)
    current_version = _semver(current)
    candidate_core = candidate_version[:3]
    current_core = current_version[:3]
    if candidate_core != current_core:
        return candidate_core > current_core
    candidate_pre = candidate_version[3]
    current_pre = current_version[3]
    if candidate_pre is None:
        return current_pre is not None
    if current_pre is None:
        return False
    return candidate_pre > current_pre


def validate_update_url(value: str, *, allow_loopback_http: bool = True) -> None:
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise UpdateError(
            "Update URLs must be HTTP(S) URLs without credentials or fragments"
        )
    if parsed.scheme == "http":
        hostname = parsed.hostname.lower()
        loopback = hostname == "localhost" or hostname in {"127.0.0.1", "::1"}
        if not allow_loopback_http or not loopback:
            raise UpdateError("Update URLs must use HTTPS unless hosted on localhost")


def _content_length(response: httpx.Response) -> int:
    value = response.headers.get("content-length")
    if value is None:
        return 0
    try:
        size = int(value)
    except ValueError as exc:
        raise UpdateError("Update response Content-Length is invalid") from exc
    if size < 0:
        raise UpdateError("Update response Content-Length is invalid")
    return size


class UpdateClient:
    def __init__(
        self,
        manifest_url: str,
        public_key: str,
        current_version: str,
        download_dir: Path,
        *,
        platform_name: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        validate_update_url(manifest_url)
        self.manifest_url = manifest_url
        self.public_key = Ed25519PublicKey.from_public_bytes(
            decode_public_key(public_key)
        )
        _semver(current_version)
        self.current_version = current_version
        self.download_dir = download_dir
        self.platform_name = platform_name or platform_key()
        self.client = httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            transport=transport,
            follow_redirects=False,
            headers={"User-Agent": f"Dayfinch-Agent/{current_version}"},
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> UpdateClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _get(self, url: str, *, stream: bool) -> httpx.Response:
        current_url = url
        for redirect_count in range(MAX_REDIRECTS + 1):
            validate_update_url(current_url)
            response = self.client.send(
                self.client.build_request("GET", current_url), stream=stream
            )
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("location", "")
            response.close()
            if not location:
                raise UpdateError("Update redirect did not include a destination")
            if redirect_count == MAX_REDIRECTS:
                raise UpdateError("Update URL exceeded the redirect limit")
            current_url = urljoin(current_url, location)
            validate_update_url(current_url)
        raise UpdateError("Update URL exceeded the redirect limit")  # pragma: no cover

    def check(self) -> AvailableUpdate | None:
        response = self._get(self.manifest_url, stream=True)
        try:
            response.raise_for_status()
            declared_size = _content_length(response)
            if declared_size > MAX_MANIFEST_BYTES:
                raise UpdateError("Update manifest is too large")
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > MAX_MANIFEST_BYTES:
                    raise UpdateError("Update manifest is too large")
        finally:
            response.close()
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise UpdateError("Update manifest is not valid JSON") from exc
        if not isinstance(payload, dict) or payload.get("schema") != 1:
            raise UpdateError("Unsupported update manifest schema")
        signature = payload.get("signature")
        if not isinstance(signature, str):
            raise UpdateError("Update manifest signature is missing")
        try:
            self.public_key.verify(
                _decode_base64url(signature, 64, "manifest signature"),
                canonical_manifest(payload),
            )
        except InvalidSignature as exc:
            raise UpdateError("Update manifest signature is invalid") from exc

        version = payload.get("version")
        published_at = payload.get("published_at")
        artifacts = payload.get("artifacts")
        if (
            not isinstance(version, str)
            or not isinstance(published_at, str)
            or not isinstance(artifacts, dict)
        ):
            raise UpdateError("Update manifest fields are invalid")
        try:
            published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise UpdateError("Update manifest published_at is invalid") from exc
        if published.tzinfo is None:
            raise UpdateError("Update manifest published_at must include a timezone")
        if not version_is_newer(version, self.current_version):
            return None
        raw_artifact = artifacts.get(self.platform_name)
        if not isinstance(raw_artifact, dict):
            raise UpdateError(
                f"Release {version} does not support {self.platform_name}"
            )
        artifact = self._parse_artifact(raw_artifact)
        return AvailableUpdate(version, published_at, artifact)

    def _parse_artifact(self, value: dict[str, Any]) -> UpdateArtifact:
        filename = value.get("filename")
        url = value.get("url")
        digest = value.get("sha256")
        size = value.get("size")
        if not isinstance(filename, str) or not _FILENAME.fullmatch(filename):
            raise UpdateError("Update artifact filename is invalid")
        if not isinstance(url, str):
            raise UpdateError("Update artifact URL is invalid")
        validate_update_url(url)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise UpdateError("Update artifact SHA-256 is invalid")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 1
            or size > MAX_ARTIFACT_BYTES
        ):
            raise UpdateError("Update artifact size is invalid")
        return UpdateArtifact(self.platform_name, filename, url, digest, size)

    def download(self, update: AvailableUpdate) -> Path:
        self.download_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.download_dir / update.artifact.filename
        if target.is_file() and self._matches(target, update.artifact):
            return target

        temporary_path: Path | None = None
        response = self._get(update.artifact.url, stream=True)
        try:
            response.raise_for_status()
            declared_size = _content_length(response)
            if declared_size and declared_size != update.artifact.size:
                raise UpdateError("Downloaded update size does not match the manifest")
            digest = hashlib.sha256()
            size = 0
            with tempfile.NamedTemporaryFile(
                dir=self.download_dir,
                prefix=f".{update.artifact.filename}.",
                suffix=".part",
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > update.artifact.size:
                        raise UpdateError(
                            "Downloaded update is larger than the manifest"
                        )
                    digest.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if size != update.artifact.size:
                raise UpdateError("Downloaded update size does not match the manifest")
            if digest.hexdigest() != update.artifact.sha256:
                raise UpdateError(
                    "Downloaded update SHA-256 does not match the manifest"
                )
            temporary_path.chmod(0o700)
            temporary_path.replace(target)
            temporary_path = None
            if hasattr(os, "O_DIRECTORY"):
                directory = os.open(self.download_dir, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            return target
        finally:
            response.close()
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _matches(path: Path, artifact: UpdateArtifact) -> bool:
        if path.stat().st_size != artifact.size:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == artifact.sha256

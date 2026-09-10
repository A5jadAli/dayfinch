from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent import __version__
from agent.update import canonical_manifest, version_is_newer

SUPPORTED_PLATFORMS = {
    "linux-x86_64",
    "linux-arm64",
    "windows-x86_64",
    "windows-arm64",
    "macos-x86_64",
    "macos-arm64",
}


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _private_key(value: str) -> Ed25519PrivateKey:
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("Signing key must be valid base64url") from exc
    if len(raw) != 32:
        raise ValueError("Signing key must decode to a 32-byte Ed25519 seed")
    return Ed25519PrivateKey.from_private_bytes(raw)


def build_manifest(
    version: str,
    base_url: str,
    artifacts: dict[str, Path],
    signing_key: str,
    *,
    published_at: str | None = None,
) -> tuple[dict, str]:
    # Parsing both sides validates the requested release without treating a
    # downgrade as an error. Equal is intentional for the package version.
    version_is_newer(version, "0.0.0")
    parsed_base_url = urlparse(base_url)
    if (
        parsed_base_url.scheme != "https"
        or not parsed_base_url.hostname
        or parsed_base_url.username
        or parsed_base_url.password
        or parsed_base_url.query
        or parsed_base_url.fragment
    ):
        raise ValueError(
            "Release base URL must be an HTTPS URL without credentials, query, or fragment"
        )
    base_url = base_url.rstrip("/")
    release_artifacts: dict[str, dict] = {}
    for target, path in sorted(artifacts.items()):
        if target not in SUPPORTED_PLATFORMS:
            raise ValueError(f"Unsupported release platform: {target}")
        if not path.is_file():
            raise ValueError(f"Release artifact not found: {path}")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", path.name):
            raise ValueError(f"Unsafe release filename: {path.name}")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        if not size:
            raise ValueError(f"Release artifact is empty: {path}")
        release_artifacts[target] = {
            "filename": path.name,
            "url": f"{base_url}/{path.name}",
            "sha256": digest.hexdigest(),
            "size": size,
        }

    private_key = _private_key(signing_key)
    payload = {
        "schema": 1,
        "version": version,
        "published_at": published_at
        or datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "artifacts": release_artifacts,
    }
    payload["signature"] = _base64url(private_key.sign(canonical_manifest(payload)))
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return payload, _base64url(public_bytes)


def _artifacts(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        target, separator, filename = value.partition("=")
        if not separator or not target or not filename or target in result:
            raise argparse.ArgumentTypeError(
                "Each --artifact must be a unique PLATFORM=FILE pair"
            )
        result[target] = Path(filename)
    if not result:
        raise argparse.ArgumentTypeError("At least one --artifact is required")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create an Ed25519-signed Dayfinch desktop update manifest"
    )
    parser.add_argument("--version", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--artifact", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--public-key-output", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.version != __version__:
        parser.error(
            f"release version {arguments.version} does not match agent {__version__}"
        )
    try:
        artifacts = _artifacts(arguments.artifact)
        signing_key = os.environ["DAYFINCH_UPDATE_SIGNING_KEY"]
        manifest, public_key = build_manifest(
            arguments.version, arguments.base_url, artifacts, signing_key
        )
    except (KeyError, ValueError, argparse.ArgumentTypeError) as exc:
        parser.error(
            "DAYFINCH_UPDATE_SIGNING_KEY is required"
            if isinstance(exc, KeyError)
            else str(exc)
        )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    arguments.public_key_output.write_text(public_key + "\n", encoding="ascii")
    arguments.output.chmod(0o644)
    arguments.public_key_output.chmod(0o644)


if __name__ == "__main__":
    main()

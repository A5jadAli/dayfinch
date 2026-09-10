import base64
import json
import stat
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent.update import (
    UpdateClient,
    UpdateError,
    platform_key,
    version_is_newer,
)
from scripts.sign_update_manifest import build_manifest


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _release(tmp_path: Path, data: bytes = b"verified desktop release"):
    artifact = tmp_path / "dayfinch-agent-linux-x64"
    artifact.write_bytes(data)
    private_key = Ed25519PrivateKey.generate()
    seed = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    manifest, public_key = build_manifest(
        "1.2.0",
        "https://releases.example.test/v1.2.0",
        {"linux-x86_64": artifact},
        _base64url(seed),
        published_at="2026-09-08T12:00:00Z",
    )
    return manifest, public_key, data


def _transport(manifest: dict, artifact: bytes) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/dayfinch-update.json":
            return httpx.Response(200, json=manifest, request=request)
        return httpx.Response(200, content=artifact, request=request)

    return httpx.MockTransport(handler)


def _updater(
    tmp_path: Path,
    manifest: dict,
    public_key: str,
    artifact: bytes,
) -> UpdateClient:
    return UpdateClient(
        "https://releases.example.test/dayfinch-update.json",
        public_key,
        "1.1.9",
        tmp_path / "updates",
        platform_name="linux-x86_64",
        transport=_transport(manifest, artifact),
    )


def test_signed_manifest_selects_platform_and_stages_verified_artifact(tmp_path):
    manifest, public_key, artifact = _release(tmp_path)

    with _updater(tmp_path, manifest, public_key, artifact) as updater:
        update = updater.check()
        assert update is not None
        assert update.version == "1.2.0"
        downloaded = updater.download(update)

    assert downloaded.read_bytes() == artifact
    assert stat.S_IMODE(downloaded.stat().st_mode) == 0o700
    assert not list(downloaded.parent.glob("*.part"))


def test_tampered_or_wrongly_signed_manifest_is_rejected(tmp_path):
    manifest, public_key, artifact = _release(tmp_path)
    manifest["version"] = "9.9.9"
    with (
        _updater(tmp_path, manifest, public_key, artifact) as updater,
        pytest.raises(UpdateError, match="signature is invalid"),
    ):
        updater.check()

    valid_manifest, _, artifact = _release(tmp_path)
    wrong_key = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    with (
        _updater(tmp_path, valid_manifest, _base64url(wrong_key), artifact) as updater,
        pytest.raises(UpdateError, match="signature is invalid"),
    ):
        updater.check()


def test_current_or_older_release_is_not_offered(tmp_path):
    manifest, public_key, artifact = _release(tmp_path)
    with UpdateClient(
        "https://releases.example.test/dayfinch-update.json",
        public_key,
        "1.2.0",
        tmp_path / "updates",
        platform_name="linux-x86_64",
        transport=_transport(manifest, artifact),
    ) as updater:
        assert updater.check() is None


def test_corrupt_download_is_removed_without_replacing_target(tmp_path):
    manifest, public_key, artifact = _release(tmp_path)
    download_dir = tmp_path / "updates"
    download_dir.mkdir()
    target = download_dir / "dayfinch-agent-linux-x64"
    target.write_bytes(b"previous safe release")

    with _updater(tmp_path, manifest, public_key, artifact + b"tampered") as updater:
        update = updater.check()
        assert update is not None
        with pytest.raises(UpdateError, match="size does not match"):
            updater.download(update)

    assert target.read_bytes() == b"previous safe release"
    assert not list(download_dir.glob("*.part"))


def test_redirect_to_plain_http_is_rejected_before_following(tmp_path):
    manifest, public_key, _artifact = _release(tmp_path)
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "http://downloads.example.test/manifest.json"},
            request=request,
        )

    with (
        UpdateClient(
            "https://releases.example.test/dayfinch-update.json",
            public_key,
            "1.1.0",
            tmp_path / "updates",
            platform_name="linux-x86_64",
            transport=httpx.MockTransport(handler),
        ) as updater,
        pytest.raises(UpdateError, match="must use HTTPS"),
    ):
        updater.check()

    assert requests == ["https://releases.example.test/dayfinch-update.json"]


def test_platform_and_semantic_version_normalization():
    assert platform_key("Linux", "AMD64") == "linux-x86_64"
    assert platform_key("Darwin", "aarch64") == "macos-arm64"
    assert version_is_newer("2.0.0", "1.99.99")
    assert version_is_newer("1.0.0", "1.0.0-rc.1")
    assert not version_is_newer("1.0.0-beta.1", "1.0.0")
    with pytest.raises(UpdateError, match="No desktop release"):
        platform_key("Plan9", "mips")


def test_manifest_serialization_contains_no_private_key(tmp_path):
    manifest, public_key, _artifact = _release(tmp_path)
    serialized = json.dumps(manifest)

    assert public_key not in serialized
    assert set(manifest) == {
        "schema",
        "version",
        "published_at",
        "artifacts",
        "signature",
    }

import base64
import hashlib
import io
import json
import tarfile

import pytest

from api.storage import ScreenshotContent
from scripts.backup_restore import (
    BackupError,
    _decrypt,
    _encrypt,
    _load_and_verify_manifest,
    _safe_extract,
    _snapshot_objects,
    encryption_key,
)


class VersionedStorage:
    def __init__(self):
        self.reads = []

    def read(self, key, version_id=None):
        self.reads.append(("screenshot", key, version_id))
        return ScreenshotContent(b"\xff\xd8\xffcapture", "image/jpeg")

    def read_blob(self, key, version_id=None):
        self.reads.append(("blob", key, version_id))
        return b"sealed-invoice"


def test_backup_encryption_round_trip_and_authentication(tmp_path):
    key = b"k" * 32
    plaintext = tmp_path / "plain.tar"
    plaintext.write_bytes(b"private backup payload" * 100)
    encrypted = tmp_path / "backup.dfbackup"
    restored = tmp_path / "restored.tar"

    _encrypt(plaintext, encrypted, key)
    assert b"private backup payload" not in encrypted.read_bytes()
    _decrypt(encrypted, restored, key)
    assert restored.read_bytes() == plaintext.read_bytes()

    damaged = bytearray(encrypted.read_bytes())
    damaged[-20] ^= 1
    encrypted.write_bytes(damaged)
    with pytest.raises(BackupError, match="authentication failed"):
        _decrypt(encrypted, tmp_path / "damaged.tar", key)


def test_backup_key_requires_32_base64url_bytes():
    encoded = base64.urlsafe_b64encode(b"z" * 32).decode().rstrip("=")
    assert encryption_key(encoded) == b"z" * 32
    with pytest.raises(BackupError, match="exactly 32 bytes"):
        encryption_key(base64.urlsafe_b64encode(b"short").decode())


def test_archive_extraction_rejects_traversal_and_links(tmp_path):
    archive_path = tmp_path / "unsafe.tar"
    with tarfile.open(archive_path, "w") as archive:
        payload = b"escape"
        member = tarfile.TarInfo("../outside")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    with pytest.raises(BackupError, match="unsafe"):
        _safe_extract(archive_path, tmp_path / "target")
    assert not (tmp_path / "outside").exists()


def test_snapshot_uses_exact_versions_and_deduplicates_objects(tmp_path):
    storage = VersionedStorage()
    rows = [
        {
            "kind": "screenshot",
            "table": "activity_records",
            "id": "record-1",
            "key": "device/capture.jpg",
            "version_id": "capture-v1",
            "content_type": "application/octet-stream",
        },
        {
            "kind": "screenshot",
            "table": "activity_records",
            "id": "record-2",
            "key": "device/capture.jpg",
            "version_id": "capture-v1",
            "content_type": "application/octet-stream",
        },
        {
            "kind": "invoice",
            "table": "invoices",
            "id": "invoice-1",
            "key": "invoices/invoice-1.dfenc",
            "version_id": "invoice-v3",
            "content_type": "application/octet-stream",
        },
    ]

    objects = _snapshot_objects(rows, storage, tmp_path)

    assert len(objects) == 2
    assert storage.reads == [
        ("screenshot", "device/capture.jpg", "capture-v1"),
        ("blob", "invoices/invoice-1.dfenc", "invoice-v3"),
    ]
    assert len(objects[0]["references"]) == 2
    assert (
        (tmp_path / objects[0]["archive_path"]).read_bytes().startswith(b"\xff\xd8\xff")
    )


def test_manifest_verification_detects_payload_tampering(tmp_path):
    dump = tmp_path / "database.dump"
    dump.write_bytes(b"database")
    digest = hashlib.sha256(b"database").hexdigest()
    manifest = {
        "format": "dayfinch-encrypted-backup",
        "version": 1,
        "database_dump": {
            "archive_path": "database.dump",
            "size": 8,
            "sha256": digest,
        },
        "objects": [],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    assert _load_and_verify_manifest(tmp_path)["version"] == 1
    dump.write_bytes(b"tampered")
    with pytest.raises(BackupError, match="integrity check failed"):
        _load_and_verify_manifest(tmp_path)

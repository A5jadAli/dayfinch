import sys
from datetime import datetime, timezone
from types import SimpleNamespace

from tracker_server.config import Settings
from tracker_server.storage import S3ScreenshotStorage


class FakeBody:
    def read(self):
        return b"\xff\xd8\xffstored"


class FakeS3Client:
    def __init__(self):
        self.put_arguments = None
        self.delete_arguments = None

    def put_object(self, **arguments):
        self.put_arguments = arguments
        return {"VersionId": "version-123"}

    def get_object(self, **_arguments):
        return {"Body": FakeBody(), "ContentType": "image/jpeg"}

    def delete_object(self, **arguments):
        self.delete_arguments = arguments
        return {}


def test_s3_storage_keeps_version_for_exact_deletion(tmp_path, monkeypatch):
    client = FakeS3Client()
    monkeypatch.setitem(
        sys.modules,
        "boto3",
        SimpleNamespace(client=lambda *_args, **_kwargs: client),
    )
    settings = Settings(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024,
        retention_days=30,
        storage_backend="s3",
        s3_bucket="private-captures",
        s3_prefix="tracker/screens",
    )
    storage = S3ScreenshotStorage(settings)
    stored = storage.save(
        "device-id",
        "record-id",
        datetime(2026, 7, 27, tzinfo=timezone.utc),
        b"\xff\xd8\xffimage",
    )
    assert stored.key == "tracker/screens/device-id/2026/07/27/record-id.jpg"
    assert stored.version_id == "version-123"
    assert client.put_arguments["ServerSideEncryption"] == "AES256"
    assert storage.read(stored.key).data == b"\xff\xd8\xffstored"

    storage.delete(stored.key, stored.version_id)
    assert client.delete_arguments == {
        "Bucket": "private-captures",
        "Key": stored.key,
        "VersionId": "version-123",
    }

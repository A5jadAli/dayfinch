from datetime import datetime, timezone

import pytest

from api.storage import ScreenshotStorage


def test_storage_accepts_images_and_confines_paths(tmp_path):
    storage = ScreenshotStorage(tmp_path / "screenshots")
    stored = storage.save(
        "device-1",
        "record-1",
        datetime.now(timezone.utc),
        b"\xff\xd8\xfffake-jpeg",
    )
    assert storage.resolve(stored.key).read_bytes() == b"\xff\xd8\xfffake-jpeg"
    assert storage.read(stored.key).content_type == "image/jpeg"
    with pytest.raises(ValueError):
        storage.resolve("../../secret.txt")


def test_storage_rejects_non_images(tmp_path):
    storage = ScreenshotStorage(tmp_path / "screenshots")
    with pytest.raises(ValueError, match="JPEG or PNG"):
        storage.save(
            "device-1", "record-1", datetime.now(timezone.utc), b"not-an-image"
        )

from datetime import UTC, datetime

import pytest

from api.storage import ScreenshotStorage


def test_storage_accepts_images_and_confines_paths(tmp_path):
    storage = ScreenshotStorage(tmp_path / "screenshots")
    stored = storage.save(
        "device-1",
        "record-1",
        datetime.now(UTC),
        b"\xff\xd8\xfffake-jpeg",
    )
    path = storage.resolve(stored.key)
    assert path.read_bytes() == b"\xff\xd8\xfffake-jpeg"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert storage.root.stat().st_mode & 0o777 == 0o700
    assert storage.read(stored.key).content_type == "image/jpeg"
    with pytest.raises(ValueError):
        storage.resolve("../../secret.txt")


def test_storage_rejects_non_images(tmp_path):
    storage = ScreenshotStorage(tmp_path / "screenshots")
    with pytest.raises(ValueError, match="JPEG or PNG"):
        storage.save("device-1", "record-1", datetime.now(UTC), b"not-an-image")

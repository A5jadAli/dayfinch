from datetime import datetime, timezone

from agent.activity import ActivitySnapshot
from agent.queue import OfflineQueue


def test_queue_is_bounded_and_acknowledges_files(tmp_path):
    queue = OfflineQueue(tmp_path / "queue", max_items=2)
    activity = ActivitySnapshot(3, 2, 100)
    records = [
        queue.add(b"jpeg-data", activity, "Editor", datetime.now(timezone.utc))
        for _ in range(3)
    ]

    assert queue.count() == 2
    assert not (tmp_path / "queue" / "images" / f"{records[0].id}.jpg").exists()

    pending = queue.pending()
    upload_fields = pending[0].fields("0.1.0")
    assert upload_fields["record_id"] == pending[0].id
    assert "id" not in upload_fields
    path = pending[0].screenshot_path
    queue.acknowledge(pending[0])
    assert queue.count() == 1
    assert not __import__("pathlib").Path(path).exists()

from datetime import UTC, datetime

from agent.activity import ActivitySnapshot
from agent.queue import OfflineQueue


def test_queue_is_bounded_and_acknowledges_files(tmp_path):
    queue = OfflineQueue(tmp_path / "queue", max_items=2)
    activity = ActivitySnapshot(3, 2, 100)
    records = [
        queue.add(b"jpeg-data", activity, "Editor", datetime.now(UTC)) for _ in range(3)
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


def test_state_events_survive_queue_reopen_and_are_acknowledged(tmp_path):
    directory = tmp_path / "queue"
    queue = OfflineQueue(directory, max_items=10)
    event = queue.add_state("active", task_id="task", heartbeat_interval_seconds=60)

    reopened = OfflineQueue(directory, max_items=10)
    assert reopened.state_count() == 1
    assert reopened.pending_states() == [event]

    reopened.acknowledge_state(event)
    assert OfflineQueue(directory, max_items=10).state_count() == 0


def test_reopen_removes_files_left_before_sqlite_commit(tmp_path):
    directory = tmp_path / "queue"
    queue = OfflineQueue(directory, max_items=10)
    orphan = queue.image_dir / "orphan.jpg"
    partial = queue.image_dir / "interrupted.jpg.part"
    orphan.write_bytes(b"private screenshot")
    partial.write_bytes(b"partial")

    OfflineQueue(directory, max_items=10)

    assert not orphan.exists()
    assert not partial.exists()

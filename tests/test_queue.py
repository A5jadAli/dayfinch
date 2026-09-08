import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.activity import ActivitySnapshot
from agent.queue import OfflineQueue

SECRET = "device-encryption-secret-that-is-long-enough"


def test_queue_is_bounded_and_acknowledges_files(tmp_path):
    queue = OfflineQueue(tmp_path / "queue", max_items=2, encryption_secret=SECRET)
    activity = ActivitySnapshot(3, 2, 100)
    records = [
        queue.add(b"jpeg-data", activity, "Editor", datetime.now(UTC)) for _ in range(3)
    ]

    assert queue.count() == 2
    assert not Path(records[0].screenshot_path).exists()

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
    queue = OfflineQueue(directory, max_items=10, encryption_secret=SECRET)
    event = queue.add_state("active", task_id="task", heartbeat_interval_seconds=60)

    reopened = OfflineQueue(directory, max_items=10, encryption_secret=SECRET)
    assert reopened.state_count() == 1
    assert reopened.pending_states() == [event]

    reopened.acknowledge_state(event)
    assert (
        OfflineQueue(directory, max_items=10, encryption_secret=SECRET).state_count()
        == 0
    )


def test_reopen_removes_files_left_before_sqlite_commit(tmp_path):
    directory = tmp_path / "queue"
    queue = OfflineQueue(directory, max_items=10, encryption_secret=SECRET)
    orphan = queue.image_dir / "orphan.jpg"
    partial = queue.image_dir / "interrupted.jpg.part"
    orphan.write_bytes(b"private screenshot")
    partial.write_bytes(b"partial")

    OfflineQueue(directory, max_items=10, encryption_secret=SECRET)

    assert not orphan.exists()
    assert not partial.exists()


def test_queue_encrypts_screenshot_application_domain_and_notes(tmp_path):
    directory = tmp_path / "queue"
    queue = OfflineQueue(directory, max_items=10, encryption_secret=SECRET)
    activity = ActivitySnapshot(4, 2, 99, focused_seconds=60, interactive_seconds=20)
    record = queue.add(
        b"private-jpeg-payload",
        activity,
        "Private Editor",
        active_url="customer.example",
    )
    event = queue.add_state("active", note="confidential work note")
    usage = queue.add_usage("Private Editor", "customer.example", 10)

    ciphertext = Path(record.screenshot_path).read_bytes()
    assert b"private-jpeg-payload" not in ciphertext
    assert queue.read_screenshot(record) == b"private-jpeg-payload"
    with sqlite3.connect(queue.database_path) as connection:
        raw_record = connection.execute(
            "SELECT active_app,active_url FROM queue WHERE id=?", (record.id,)
        ).fetchone()
        raw_note = connection.execute(
            "SELECT note FROM state_events WHERE id=?", (event.id,)
        ).fetchone()[0]
        raw_usage = connection.execute(
            "SELECT active_app,active_url FROM usage_events WHERE id=?", (usage.id,)
        ).fetchone()
    assert "Private Editor" not in raw_record[0]
    assert "customer.example" not in raw_record[1]
    assert "confidential" not in raw_note
    assert "Private Editor" not in raw_usage[0]
    assert "customer.example" not in raw_usage[1]
    assert queue.pending()[0].active_url == "customer.example"
    assert queue.pending_states()[0].note == "confidential work note"
    assert queue.pending_usage()[0].active_url == "customer.example"


def test_queue_rejects_wrong_key_and_tampered_ciphertext(tmp_path):
    directory = tmp_path / "queue"
    queue = OfflineQueue(directory, max_items=10, encryption_secret=SECRET)
    record = queue.add(b"screenshot", ActivitySnapshot(0, 0, 0), "Editor")

    with pytest.raises(ValueError, match="key does not match"):
        OfflineQueue(
            directory,
            max_items=10,
            encryption_secret="different-device-secret-that-is-long-enough",
        )

    content = bytearray(Path(record.screenshot_path).read_bytes())
    content[-1] ^= 1
    Path(record.screenshot_path).write_bytes(content)
    with pytest.raises(ValueError, match="authentication"):
        queue.read_screenshot(record)

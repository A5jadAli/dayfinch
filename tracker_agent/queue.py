from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .activity import ActivitySnapshot


@dataclass(frozen=True)
class QueuedRecord:
    id: str
    captured_at: str
    keyboard_events: int
    mouse_clicks: int
    mouse_distance: int
    focused_seconds: int
    interactive_seconds: int
    session_id: str
    active_app: str
    screenshot_path: str

    def fields(self, agent_version: str) -> dict[str, str]:
        values = asdict(self)
        values.pop("screenshot_path")
        values["record_id"] = values.pop("id")
        values["agent_version"] = agent_version
        return {key: str(value) for key, value in values.items()}


class OfflineQueue:
    def __init__(self, directory: Path, max_items: int):
        self.directory = directory
        self.image_dir = directory / "images"
        self.database_path = directory / "queue.sqlite3"
        self.max_items = max_items
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS queue (
                    id TEXT PRIMARY KEY,
                    captured_at TEXT NOT NULL,
                    keyboard_events INTEGER NOT NULL,
                    mouse_clicks INTEGER NOT NULL,
                    mouse_distance INTEGER NOT NULL,
                    focused_seconds INTEGER NOT NULL DEFAULT 0,
                    interactive_seconds INTEGER NOT NULL DEFAULT 0,
                    session_id TEXT NOT NULL DEFAULT '',
                    active_app TEXT NOT NULL,
                    screenshot_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(queue)")}
            if "focused_seconds" not in columns:
                connection.execute("ALTER TABLE queue ADD COLUMN focused_seconds INTEGER NOT NULL DEFAULT 0")
            if "interactive_seconds" not in columns:
                connection.execute("ALTER TABLE queue ADD COLUMN interactive_seconds INTEGER NOT NULL DEFAULT 0")
            if "session_id" not in columns:
                connection.execute("ALTER TABLE queue ADD COLUMN session_id TEXT NOT NULL DEFAULT ''")

    def add(
        self,
        screenshot: bytes,
        activity: ActivitySnapshot,
        active_app: str,
        captured_at: datetime | None = None,
        session_id: str = "",
    ) -> QueuedRecord:
        record_id = str(uuid.uuid4())
        captured_at = captured_at or datetime.now(timezone.utc)
        image_path = self.image_dir / f"{record_id}.jpg"
        temporary = image_path.with_suffix(".jpg.part")
        with temporary.open("wb") as stream:
            stream.write(screenshot)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, image_path)
        record = QueuedRecord(
            id=record_id,
            captured_at=captured_at.isoformat(),
            keyboard_events=activity.keyboard_events,
            mouse_clicks=activity.mouse_clicks,
            mouse_distance=activity.mouse_distance,
            focused_seconds=activity.focused_seconds,
            interactive_seconds=activity.interactive_seconds,
            session_id=session_id,
            active_app=active_app[:160],
            screenshot_path=str(image_path),
        )
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO queue(
                       id, captured_at, keyboard_events, mouse_clicks, mouse_distance,
                       focused_seconds, interactive_seconds, session_id, active_app,
                       screenshot_path, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.id,
                    record.captured_at,
                    record.keyboard_events,
                    record.mouse_clicks,
                    record.mouse_distance,
                    record.focused_seconds,
                    record.interactive_seconds,
                    record.session_id,
                    record.active_app,
                    record.screenshot_path,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        self._trim()
        return record

    def pending(self, limit: int = 10) -> list[QueuedRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM queue ORDER BY created_at ASC LIMIT ?", (max(1, limit),)
            ).fetchall()
        return [
            QueuedRecord(
                id=row["id"],
                captured_at=row["captured_at"],
                keyboard_events=row["keyboard_events"],
                mouse_clicks=row["mouse_clicks"],
                mouse_distance=row["mouse_distance"],
                focused_seconds=row["focused_seconds"],
                interactive_seconds=row["interactive_seconds"],
                session_id=row["session_id"],
                active_app=row["active_app"],
                screenshot_path=row["screenshot_path"],
            )
            for row in rows
        ]

    def acknowledge(self, record: QueuedRecord) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM queue WHERE id = ?", (record.id,))
        Path(record.screenshot_path).unlink(missing_ok=True)

    def count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM queue").fetchone()[0])

    def _trim(self) -> None:
        with self._connect() as connection:
            excess = connection.execute(
                """SELECT id, screenshot_path FROM queue ORDER BY created_at ASC
                   LIMIT MAX((SELECT COUNT(*) FROM queue) - ?, 0)""",
                (self.max_items,),
            ).fetchall()
            connection.executemany("DELETE FROM queue WHERE id = ?", [(row["id"],) for row in excess])
        for row in excess:
            Path(row["screenshot_path"]).unlink(missing_ok=True)

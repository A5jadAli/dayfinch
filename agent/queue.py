from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

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
    active_url: str = ""
    automation_suspected: bool = False

    def fields(self, agent_version: str) -> dict[str, str]:
        values = asdict(self)
        values.pop("screenshot_path")
        values["record_id"] = values.pop("id")
        values["agent_version"] = agent_version
        values["automation_suspected"] = "1" if self.automation_suspected else "0"
        return {key: str(value) for key, value in values.items()}


@dataclass(frozen=True)
class StateEvent:
    id: str
    observed_at: str
    status: str
    task_id: str
    idle_seconds: int
    heartbeat_interval_seconds: int


class OfflineQueue:
    def __init__(self, directory: Path, max_items: int):
        self.directory = directory
        self.image_dir = directory / "images"
        self.database_path = directory / "queue.sqlite3"
        self.max_items = max_items
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self._pending = 0
        self._pending_states = 0
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        # FULL makes acknowledged state transitions durable across an abrupt power
        # loss. WAL keeps those small writes from blocking screenshot reads.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
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
                    active_url TEXT NOT NULL DEFAULT '',
                    automation_suspected INTEGER NOT NULL DEFAULT 0,
                    screenshot_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS state_events (
                    id TEXT PRIMARY KEY,
                    observed_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active', 'paused', 'stopped')),
                    task_id TEXT NOT NULL DEFAULT '',
                    idle_seconds INTEGER NOT NULL DEFAULT 0,
                    heartbeat_interval_seconds INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(queue)")}
            if "focused_seconds" not in columns:
                connection.execute(
                    "ALTER TABLE queue ADD COLUMN focused_seconds INTEGER NOT NULL DEFAULT 0"
                )
            if "interactive_seconds" not in columns:
                connection.execute(
                    "ALTER TABLE queue ADD COLUMN interactive_seconds INTEGER NOT NULL DEFAULT 0"
                )
            if "session_id" not in columns:
                connection.execute(
                    "ALTER TABLE queue ADD COLUMN session_id TEXT NOT NULL DEFAULT ''"
                )
            if "active_url" not in columns:
                connection.execute(
                    "ALTER TABLE queue ADD COLUMN active_url TEXT NOT NULL DEFAULT ''"
                )
            if "automation_suspected" not in columns:
                connection.execute(
                    "ALTER TABLE queue ADD COLUMN automation_suspected INTEGER NOT NULL DEFAULT 0"
                )
            self._pending = int(
                connection.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
            )
            self._pending_states = int(
                connection.execute("SELECT COUNT(*) FROM state_events").fetchone()[0]
            )
            referenced = {
                Path(row["screenshot_path"])
                for row in connection.execute(
                    "SELECT screenshot_path FROM queue"
                ).fetchall()
            }
        # A crash between atomic rename and SQLite commit can leave an unreferenced
        # image. It contains sensitive screen data, so do not retain it indefinitely.
        for image in self.image_dir.glob("*.jpg"):
            if image not in referenced:
                image.unlink(missing_ok=True)
        for partial in self.image_dir.glob("*.part"):
            partial.unlink(missing_ok=True)

    def add_state(
        self,
        status: str,
        *,
        task_id: str = "",
        idle_seconds: int = 0,
        heartbeat_interval_seconds: int = 60,
        observed_at: datetime | None = None,
    ) -> StateEvent:
        """Journal a time-state event before attempting any network request."""
        if status not in {"active", "paused", "stopped"}:
            raise ValueError("invalid state event status")
        event = StateEvent(
            id=str(uuid.uuid4()),
            observed_at=(observed_at or datetime.now(UTC)).isoformat(),
            status=status,
            task_id=task_id,
            idle_seconds=max(0, int(idle_seconds)),
            heartbeat_interval_seconds=max(1, int(heartbeat_interval_seconds)),
        )
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO state_events(
                       id, observed_at, status, task_id, idle_seconds,
                       heartbeat_interval_seconds, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.id,
                    event.observed_at,
                    event.status,
                    event.task_id,
                    event.idle_seconds,
                    event.heartbeat_interval_seconds,
                    datetime.now(UTC).isoformat(),
                ),
            )
        self._pending_states += 1
        return event

    def pending_states(self, limit: int = 10) -> list[StateEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM state_events
                   ORDER BY observed_at ASC, created_at ASC LIMIT ?""",
                (max(1, limit),),
            ).fetchall()
        return [
            StateEvent(
                id=row["id"],
                observed_at=row["observed_at"],
                status=row["status"],
                task_id=row["task_id"],
                idle_seconds=row["idle_seconds"],
                heartbeat_interval_seconds=row["heartbeat_interval_seconds"],
            )
            for row in rows
        ]

    def acknowledge_state(self, event: StateEvent) -> None:
        with self._connect() as connection:
            deleted = connection.execute(
                "DELETE FROM state_events WHERE id = ?", (event.id,)
            ).rowcount
        self._pending_states = max(0, self._pending_states - max(0, deleted))

    def state_count(self) -> int:
        return self._pending_states

    def add(
        self,
        screenshot: bytes,
        activity: ActivitySnapshot,
        active_app: str,
        captured_at: datetime | None = None,
        session_id: str = "",
        active_url: str = "",
    ) -> QueuedRecord:
        record_id = str(uuid.uuid4())
        captured_at = captured_at or datetime.now(UTC)
        image_path = self.image_dir / f"{record_id}.jpg"
        temporary = image_path.with_suffix(".jpg.part")
        with temporary.open("wb") as stream:
            stream.write(screenshot)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, image_path)
        if os.name != "nt":
            directory_fd = os.open(self.image_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
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
            active_url=active_url[:255],
            automation_suspected=activity.automation_suspected,
        )
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO queue(
                       id, captured_at, keyboard_events, mouse_clicks, mouse_distance,
                       focused_seconds, interactive_seconds, session_id, active_app,
                       active_url, automation_suspected, screenshot_path, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    record.active_url,
                    1 if record.automation_suspected else 0,
                    record.screenshot_path,
                    datetime.now(UTC).isoformat(),
                ),
            )
        self._pending += 1
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
                active_url=row["active_url"],
                automation_suspected=bool(row["automation_suspected"]),
            )
            for row in rows
        ]

    def acknowledge(self, record: QueuedRecord) -> None:
        with self._connect() as connection:
            deleted = connection.execute(
                "DELETE FROM queue WHERE id = ?", (record.id,)
            ).rowcount
        self._pending = max(0, self._pending - max(0, deleted))
        Path(record.screenshot_path).unlink(missing_ok=True)

    def count(self) -> int:
        """Cached so the idle loop never queries SQLite just to render status."""
        return self._pending

    def _trim(self) -> None:
        with self._connect() as connection:
            excess = connection.execute(
                """SELECT id, screenshot_path FROM queue ORDER BY created_at ASC
                   LIMIT MAX((SELECT COUNT(*) FROM queue) - ?, 0)""",
                (self.max_items,),
            ).fetchall()
            connection.executemany(
                "DELETE FROM queue WHERE id = ?", [(row["id"],) for row in excess]
            )
        self._pending = max(0, self._pending - len(excess))
        for row in excess:
            Path(row["screenshot_path"]).unlink(missing_ok=True)

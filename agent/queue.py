from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .activity import ActivitySnapshot

QUEUE_MAGIC = b"DFQ1"
TEXT_PREFIX = "dfenc1:"


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
    project_id: str
    note: str
    idle_seconds: int
    heartbeat_interval_seconds: int


@dataclass(frozen=True)
class UsageEvent:
    id: str
    observed_at: str
    active_app: str
    active_url: str
    focused_seconds: int


class OfflineQueue:
    def __init__(self, directory: Path, max_items: int, encryption_secret: str):
        if len(encryption_secret) < 32:
            raise ValueError(
                "offline queue encryption secret must be at least 32 characters"
            )
        self.directory = directory
        self.image_dir = directory / "images"
        self.quarantine_dir = directory / "quarantine"
        self.database_path = directory / "queue.sqlite3"
        self.max_items = max_items
        self.directory.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(
            b"dayfinch:offline-queue:v1\0" + encryption_secret.encode()
        ).digest()
        self._aead = AESGCM(key)
        self._verify_key(key)
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.directory, 0o700)
            os.chmod(self.image_dir, 0o700)
            os.chmod(self.quarantine_dir, 0o700)
        self._pending = 0
        self._pending_states = 0
        self._pending_usage = 0
        self._quarantined = 0
        self._initialize()
        self._quarantined = len(list(self.quarantine_dir.glob("*.meta.dfq")))
        if os.name != "nt":
            os.chmod(self.database_path, 0o600)

    def _verify_key(self, key: bytes) -> None:
        path = self.directory / "queue.keycheck"
        expected = hmac.new(
            key, b"dayfinch-queue-key-check-v1", hashlib.sha256
        ).hexdigest()
        if path.exists():
            if not hmac.compare_digest(
                path.read_text(encoding="ascii").strip(), expected
            ):
                raise ValueError(
                    "offline queue key does not match; restore the previous device token"
                )
            return
        with path.open("w", encoding="ascii") as stream:
            stream.write(expected)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(path, 0o600)

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
        legacy_paths: list[Path] = []
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
                CREATE TABLE IF NOT EXISTS usage_events (
                    id TEXT PRIMARY KEY,
                    observed_at TEXT NOT NULL,
                    active_app TEXT NOT NULL DEFAULT '',
                    active_url TEXT NOT NULL DEFAULT '',
                    focused_seconds INTEGER NOT NULL DEFAULT 0,
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
                    project_id TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
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
            state_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(state_events)")
            }
            if "project_id" not in state_columns:
                connection.execute(
                    "ALTER TABLE state_events ADD COLUMN project_id TEXT NOT NULL DEFAULT ''"
                )
            if "note" not in state_columns:
                connection.execute(
                    "ALTER TABLE state_events ADD COLUMN note TEXT NOT NULL DEFAULT ''"
                )
            for row in connection.execute(
                "SELECT id,active_app,active_url,screenshot_path FROM queue"
            ).fetchall():
                updates: dict[str, str] = {}
                for column in ("active_app", "active_url"):
                    value = row[column]
                    if value and not value.startswith(TEXT_PREFIX):
                        updates[column] = self._seal_text(value, row["id"], column)
                path = Path(row["screenshot_path"])
                if path.suffix != ".dfq" and path.exists():
                    encrypted_path = self.image_dir / f"{row['id']}.dfq"
                    self._atomic_write(
                        encrypted_path,
                        self._seal_bytes(path.read_bytes(), row["id"].encode()),
                    )
                    updates["screenshot_path"] = str(encrypted_path)
                    legacy_paths.append(path)
                if updates:
                    assignments = ",".join(f"{key}=?" for key in updates)
                    connection.execute(
                        f"UPDATE queue SET {assignments} WHERE id=?",
                        (*updates.values(), row["id"]),
                    )
            for row in connection.execute(
                "SELECT id,note FROM state_events WHERE note <> ''"
            ).fetchall():
                if not row["note"].startswith(TEXT_PREFIX):
                    connection.execute(
                        "UPDATE state_events SET note=? WHERE id=?",
                        (self._seal_text(row["note"], row["id"], "note"), row["id"]),
                    )
            for row in connection.execute(
                "SELECT id,active_app,active_url FROM usage_events"
            ).fetchall():
                updates = {}
                for column in ("active_app", "active_url"):
                    value = row[column]
                    if value and not value.startswith(TEXT_PREFIX):
                        updates[column] = self._seal_text(value, row["id"], column)
                if updates:
                    assignments = ",".join(f"{key}=?" for key in updates)
                    connection.execute(
                        f"UPDATE usage_events SET {assignments} WHERE id=?",
                        (*updates.values(), row["id"]),
                    )
            self._pending = int(
                connection.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
            )
            self._pending_states = int(
                connection.execute("SELECT COUNT(*) FROM state_events").fetchone()[0]
            )
            self._pending_usage = int(
                connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
            )
            referenced = {
                Path(row["screenshot_path"])
                for row in connection.execute(
                    "SELECT screenshot_path FROM queue"
                ).fetchall()
            }
        for path in legacy_paths:
            path.unlink(missing_ok=True)
        # A crash between atomic rename and SQLite commit can leave an unreferenced
        # image. It contains sensitive screen data, so do not retain it indefinitely.
        for image in self.image_dir.iterdir():
            if not image.is_file() or image.suffix == ".part":
                continue
            if image not in referenced:
                image.unlink(missing_ok=True)
        for partial in self.image_dir.glob("*.part"):
            partial.unlink(missing_ok=True)

    def add_state(
        self,
        status: str,
        *,
        task_id: str = "",
        project_id: str = "",
        note: str = "",
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
            project_id=project_id,
            note=note[:500],
            idle_seconds=max(0, int(idle_seconds)),
            heartbeat_interval_seconds=max(1, int(heartbeat_interval_seconds)),
        )
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO state_events(
                       id, observed_at, status, task_id, project_id, note, idle_seconds,
                       heartbeat_interval_seconds, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.id,
                    event.observed_at,
                    event.status,
                    event.task_id,
                    event.project_id,
                    self._seal_text(event.note, event.id, "note") if event.note else "",
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
                project_id=row["project_id"],
                note=self._open_text(row["note"], row["id"], "note"),
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

    def add_usage(
        self,
        active_app: str,
        active_url: str,
        focused_seconds: int,
        observed_at: datetime | None = None,
    ) -> UsageEvent:
        event = UsageEvent(
            id=str(uuid.uuid4()),
            observed_at=(observed_at or datetime.now(UTC)).isoformat(),
            active_app=active_app[:160],
            active_url=active_url[:255],
            focused_seconds=max(0, min(3600, int(focused_seconds))),
        )
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO usage_events(
                       id,observed_at,active_app,active_url,focused_seconds,created_at
                   ) VALUES (?,?,?,?,?,?)""",
                (
                    event.id,
                    event.observed_at,
                    self._seal_text(event.active_app, event.id, "active_app"),
                    self._seal_text(event.active_url, event.id, "active_url"),
                    event.focused_seconds,
                    datetime.now(UTC).isoformat(),
                ),
            )
        self._pending_usage += 1
        return event

    def pending_usage(self, limit: int = 10) -> list[UsageEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM usage_events ORDER BY observed_at,created_at LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [
            UsageEvent(
                id=row["id"],
                observed_at=row["observed_at"],
                active_app=self._open_text(row["active_app"], row["id"], "active_app"),
                active_url=self._open_text(row["active_url"], row["id"], "active_url"),
                focused_seconds=row["focused_seconds"],
            )
            for row in rows
        ]

    def acknowledge_usage(self, event: UsageEvent) -> None:
        with self._connect() as connection:
            deleted = connection.execute(
                "DELETE FROM usage_events WHERE id=?", (event.id,)
            ).rowcount
        self._pending_usage = max(0, self._pending_usage - max(0, deleted))

    def usage_count(self) -> int:
        return self._pending_usage

    def quarantine_record(self, record: QueuedRecord, reason: str) -> None:
        source = Path(record.screenshot_path)
        if source.exists():
            os.replace(source, self.quarantine_dir / f"{record.id}.screenshot.dfq")
        payload = asdict(record)
        payload.pop("screenshot_path", None)
        self._write_quarantine("screenshot", record.id, payload, reason)
        with self._connect() as connection:
            deleted = connection.execute(
                "DELETE FROM queue WHERE id=?", (record.id,)
            ).rowcount
        self._pending = max(0, self._pending - max(0, deleted))

    def quarantine_state(self, event: StateEvent, reason: str) -> None:
        self._write_quarantine("state", event.id, asdict(event), reason)
        with self._connect() as connection:
            deleted = connection.execute(
                "DELETE FROM state_events WHERE id=?", (event.id,)
            ).rowcount
        self._pending_states = max(0, self._pending_states - max(0, deleted))

    def quarantine_usage(self, event: UsageEvent, reason: str) -> None:
        self._write_quarantine("usage", event.id, asdict(event), reason)
        with self._connect() as connection:
            deleted = connection.execute(
                "DELETE FROM usage_events WHERE id=?", (event.id,)
            ).rowcount
        self._pending_usage = max(0, self._pending_usage - max(0, deleted))

    def quarantine_count(self) -> int:
        return self._quarantined

    def _write_quarantine(
        self, kind: str, item_id: str, payload: dict, reason: str
    ) -> None:
        envelope = json.dumps(
            {
                "kind": kind,
                "id": item_id,
                "reason": reason[:1000],
                "payload": payload,
                "quarantined_at": datetime.now(UTC).isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        destination = self.quarantine_dir / f"{item_id}.meta.dfq"
        self._atomic_write(
            destination,
            self._seal_bytes(envelope, f"quarantine:{item_id}".encode()),
        )
        self._quarantined += 1

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
        image_path = self.image_dir / f"{record_id}.dfq"
        self._atomic_write(image_path, self._seal_bytes(screenshot, record_id.encode()))
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
                    self._seal_text(record.active_app, record.id, "active_app"),
                    self._seal_text(record.active_url, record.id, "active_url"),
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
                active_app=self._open_text(row["active_app"], row["id"], "active_app"),
                screenshot_path=row["screenshot_path"],
                active_url=self._open_text(row["active_url"], row["id"], "active_url"),
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

    def read_screenshot(self, record: QueuedRecord) -> bytes:
        data = Path(record.screenshot_path).read_bytes()
        try:
            return self._open_bytes(data, record.id.encode())
        except InvalidTag as exc:
            raise ValueError("offline screenshot authentication failed") from exc

    def _seal_text(self, value: str, record_id: str, field: str) -> str:
        if not value:
            return ""
        encrypted = self._seal_bytes(value.encode(), f"{record_id}:{field}".encode())
        return TEXT_PREFIX + base64.urlsafe_b64encode(encrypted).decode()

    def _open_text(self, value: str, record_id: str, field: str) -> str:
        if not value or not value.startswith(TEXT_PREFIX):
            return value
        try:
            encrypted = base64.urlsafe_b64decode(value[len(TEXT_PREFIX) :])
            return self._open_bytes(encrypted, f"{record_id}:{field}".encode()).decode()
        except (InvalidTag, ValueError, UnicodeDecodeError) as exc:
            raise ValueError("offline queue metadata authentication failed") from exc

    def _seal_bytes(self, value: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(12)
        return QUEUE_MAGIC + nonce + self._aead.encrypt(nonce, value, aad)

    def _open_bytes(self, value: bytes, aad: bytes) -> bytes:
        if not value.startswith(QUEUE_MAGIC) or len(value) < len(QUEUE_MAGIC) + 13:
            raise ValueError("offline queue ciphertext format is invalid")
        offset = len(QUEUE_MAGIC)
        return self._aead.decrypt(
            value[offset : offset + 12], value[offset + 12 :], aad
        )

    def _atomic_write(self, destination: Path, value: bytes) -> None:
        temporary = destination.with_suffix(destination.suffix + ".part")
        with temporary.open("wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        if os.name != "nt":
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

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

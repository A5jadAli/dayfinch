from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .migrations import apply_migrations
from .repositories.accounts import AccountsRepository
from .repositories.activity import ActivityRepository
from .repositories.audit import AuditRepository
from .repositories.devices import DevicesRepository
from .repositories.projects import ProjectsRepository
from .repositories.work import WorkRepository


class Database(
    AccountsRepository,
    ProjectsRepository,
    DevicesRepository,
    ActivityRepository,
    AuditRepository,
    WorkRepository,
):
    """Connection owner and backwards-compatible repository facade."""

    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            apply_migrations(connection)

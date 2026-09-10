from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from uuid import UUID

from psycopg import Connection
from psycopg_pool import ConnectionPool

from .migrations import apply_migrations
from .repositories.accounts import AccountsRepository
from .repositories.activity import ActivityRepository
from .repositories.audit import AuditRepository
from .repositories.automatic_tracking import AutomaticTrackingRepository
from .repositories.background_jobs import BackgroundJobsRepository
from .repositories.devices import DevicesRepository
from .repositories.integrations import IntegrationsRepository
from .repositories.projects import ProjectsRepository
from .repositories.timesheets import TimesheetRepository
from .repositories.work import WorkRepository
from .repositories.workforce import WorkforceRepository


def normalized_dict_row(cursor):
    columns = [column.name for column in cursor.description or ()]

    def make_row(values):
        return {
            column: value.isoformat()
            if isinstance(value, datetime)
            else str(value)
            if isinstance(value, UUID)
            else value
            for column, value in zip(columns, values, strict=True)
        }

    return make_row


class Database(
    AccountsRepository,
    AutomaticTrackingRepository,
    ProjectsRepository,
    DevicesRepository,
    IntegrationsRepository,
    ActivityRepository,
    AuditRepository,
    BackgroundJobsRepository,
    WorkRepository,
    TimesheetRepository,
    WorkforceRepository,
):
    """PostgreSQL connection-pool owner and repository facade."""

    def __init__(
        self, database_url: str, *, min_pool_size: int = 1, max_pool_size: int = 10
    ):
        self.pool = ConnectionPool(
            conninfo=database_url,
            min_size=min_pool_size,
            max_size=max_pool_size,
            kwargs={"row_factory": normalized_dict_row},
            open=False,
        )

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        with self.pool.connection() as connection, connection.transaction():
            yield connection

    def initialize(self) -> None:
        self.pool.open(wait=True)
        with self.connect() as connection:
            apply_migrations(connection)

    def close(self) -> None:
        self.pool.close()

    def ping(self) -> None:
        with self.connect() as connection:
            connection.execute("SELECT 1").fetchone()

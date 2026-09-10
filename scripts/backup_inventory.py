"""Print a secret-free inventory for backup/restore comparison."""

from __future__ import annotations

import hashlib
import json

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from api.config import Settings
from api.storage import ScreenshotStore, create_storage
from scripts.backup_restore import _database_name, _object_rows


def _payload(storage: ScreenshotStore, row: dict[str, object]) -> bytes:
    key = str(row["key"])
    version_id = str(row["version_id"]) if row.get("version_id") else None
    if row["kind"] == "screenshot":
        return storage.read(key, version_id).data
    return storage.read_blob(key, version_id)


def create_inventory(settings: Settings) -> dict[str, object]:
    storage = create_storage(settings)
    with psycopg.connect(settings.database_url, row_factory=dict_row) as connection:
        table_names = [
            str(row["tablename"])
            for row in connection.execute(
                """SELECT tablename FROM pg_tables
                     WHERE schemaname='public' ORDER BY tablename"""
            ).fetchall()
        ]
        tables = {
            name: int(
                connection.execute(
                    sql.SQL("SELECT COUNT(*) AS count FROM {}").format(
                        sql.Identifier(name)
                    )
                ).fetchone()["count"]
            )
            for name in table_names
        }
        rows = _object_rows(connection)

    objects = []
    for row in rows:
        data = _payload(storage, row)
        objects.append(
            {
                "kind": str(row["kind"]),
                "table": str(row["table"]),
                "id": str(row["id"]),
                "key": str(row["key"]),
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    objects.sort(key=lambda item: (item["table"], item["id"]))
    return {
        "database_name": _database_name(settings.database_url),
        "tables": tables,
        "objects": objects,
    }


def main() -> None:
    settings = Settings.from_env()
    settings.prepare()
    print(json.dumps(create_inventory(settings), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qsl, unquote, urlparse

import psycopg
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from psycopg.rows import dict_row

from api.config import Settings
from api.storage import ScreenshotStore, create_storage

MAGIC = b"DFBACKUP1"
FORMAT_VERSION = 1
CHUNK_SIZE = 1024 * 1024
TAG_SIZE = 16
KEY_ENVIRONMENT_VARIABLE = "TRACKER_BACKUP_ENCRYPTION_KEY"


class BackupError(RuntimeError):
    pass


def encryption_key(value: str | None = None) -> bytes:
    configured = (value or os.getenv(KEY_ENVIRONMENT_VARIABLE, "")).strip()
    if not configured:
        raise BackupError(f"{KEY_ENVIRONMENT_VARIABLE} is required")
    try:
        decoded = base64.urlsafe_b64decode(configured + "=" * (-len(configured) % 4))
    except (ValueError, TypeError) as exc:
        raise BackupError(f"{KEY_ENVIRONMENT_VARIABLE} is not valid base64") from exc
    if len(decoded) != 32:
        raise BackupError(f"{KEY_ENVIRONMENT_VARIABLE} must decode to exactly 32 bytes")
    return decoded


def _encrypt(source: Path, destination: Path, key: bytes) -> None:
    nonce = os.urandom(12)
    authenticated_header = MAGIC + nonce
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(authenticated_header)
    try:
        with source.open("rb") as incoming, destination.open("xb") as outgoing:
            os.chmod(destination, 0o600)
            outgoing.write(authenticated_header)
            while chunk := incoming.read(CHUNK_SIZE):
                outgoing.write(encryptor.update(chunk))
            outgoing.write(encryptor.finalize())
            outgoing.write(encryptor.tag)
            outgoing.flush()
            os.fsync(outgoing.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _decrypt(source: Path, destination: Path, key: bytes) -> None:
    total_size = source.stat().st_size
    minimum_size = len(MAGIC) + 12 + TAG_SIZE
    if total_size < minimum_size:
        raise BackupError("Backup archive is truncated")
    with source.open("rb") as incoming:
        header = incoming.read(len(MAGIC) + 12)
        if not header.startswith(MAGIC):
            raise BackupError("Backup archive format is invalid")
        nonce = header[len(MAGIC) :]
        incoming.seek(-TAG_SIZE, os.SEEK_END)
        tag = incoming.read(TAG_SIZE)
        ciphertext_size = total_size - len(header) - TAG_SIZE
        incoming.seek(len(header))
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(header)
        try:
            with destination.open("xb") as outgoing:
                os.chmod(destination, 0o600)
                remaining = ciphertext_size
                while remaining:
                    chunk = incoming.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        raise BackupError("Backup archive is truncated")
                    remaining -= len(chunk)
                    outgoing.write(decryptor.update(chunk))
                outgoing.write(decryptor.finalize())
                outgoing.flush()
                os.fsync(outgoing.fileno())
        except InvalidTag as exc:
            destination.unlink(missing_ok=True)
            raise BackupError(
                "Backup authentication failed (wrong key or corrupted archive)"
            ) from exc
        except Exception:
            destination.unlink(missing_ok=True)
            raise


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _database_name(database_url: str) -> str:
    parsed = urlparse(database_url)
    name = unquote(parsed.path.removeprefix("/"))
    if not name or "/" in name:
        raise BackupError("Database URL must contain exactly one database name")
    return name


def _postgres_environment(database_url: str) -> dict[str, str]:
    parsed = urlparse(database_url)
    environment = os.environ.copy()
    values = {
        "PGHOST": parsed.hostname,
        "PGPORT": str(parsed.port or 5432),
        "PGUSER": unquote(parsed.username or ""),
        "PGPASSWORD": unquote(parsed.password or ""),
        "PGDATABASE": _database_name(database_url),
    }
    for key, value in parse_qsl(parsed.query, keep_blank_values=False):
        environment_key = {
            "sslmode": "PGSSLMODE",
            "sslrootcert": "PGSSLROOTCERT",
            "sslcert": "PGSSLCERT",
            "sslkey": "PGSSLKEY",
            "connect_timeout": "PGCONNECT_TIMEOUT",
        }.get(key)
        if environment_key:
            values[environment_key] = value
    environment.update({key: value for key, value in values.items() if value})
    return environment


def _require_program(name: str) -> None:
    if not shutil.which(name):
        raise BackupError(f"{name} is required and was not found on PATH")


def _run(command: list[str], database_url: str) -> None:
    try:
        subprocess.run(
            command,
            check=True,
            env=_postgres_environment(database_url),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as exc:
        raise BackupError(
            f"{command[0]} failed with exit code {exc.returncode}"
        ) from exc


def _object_rows(connection: psycopg.Connection) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    screenshots = connection.execute(
        """SELECT id::text, screenshot_path AS object_key,
                  storage_version_id AS version_id
             FROM activity_records
            WHERE screenshot_path <> ''
            ORDER BY id"""
    ).fetchall()
    for row in screenshots:
        rows.append(
            {
                "kind": "screenshot",
                "table": "activity_records",
                "id": row["id"],
                "key": row["object_key"],
                "version_id": row["version_id"],
                "content_type": "application/octet-stream",
            }
        )
    invoices = connection.execute(
        """SELECT id::text, encrypted_document_key AS object_key,
                  document_version_id AS version_id,
                  encrypted_document_sha256 AS expected_sha256
             FROM invoices
            WHERE encrypted_document_key <> ''
            ORDER BY id"""
    ).fetchall()
    for row in invoices:
        rows.append(
            {
                "kind": "invoice",
                "table": "invoices",
                "id": row["id"],
                "key": row["object_key"],
                "version_id": row["version_id"],
                "expected_sha256": row["expected_sha256"],
                "content_type": "application/octet-stream",
            }
        )
    team_invoices = connection.execute(
        """SELECT id::text, encrypted_document_key AS object_key,
                  document_version_id AS version_id,
                  encrypted_document_sha256 AS expected_sha256
             FROM team_invoices
            WHERE encrypted_document_key <> ''
            ORDER BY id"""
    ).fetchall()
    for row in team_invoices:
        rows.append(
            {
                "kind": "invoice",
                "table": "team_invoices",
                "id": row["id"],
                "key": row["object_key"],
                "version_id": row["version_id"],
                "expected_sha256": row["expected_sha256"],
                "content_type": "application/octet-stream",
            }
        )
    return rows


def _archive_object_path(key: str, version_id: str | None) -> str:
    identity = f"{key}\0{version_id or ''}".encode()
    return f"objects/{hashlib.sha256(identity).hexdigest()}.blob"


def _snapshot_objects(
    rows: Iterable[dict[str, object]], storage: ScreenshotStore, root: Path
) -> list[dict[str, object]]:
    objects: dict[tuple[str, str | None], dict[str, object]] = {}
    for row in rows:
        key = str(row["key"])
        version_id = str(row["version_id"]) if row.get("version_id") else None
        identity = (key, version_id)
        reference = {"table": str(row["table"]), "id": str(row["id"])}
        if identity in objects:
            objects[identity]["references"].append(reference)  # type: ignore[union-attr]
            continue
        if row["kind"] == "screenshot":
            content = storage.read(key, version_id)
            data = content.data
            content_type = content.content_type
        else:
            data = storage.read_blob(key, version_id)
            content_type = str(row["content_type"])
        digest = hashlib.sha256(data).hexdigest()
        expected_digest = str(row.get("expected_sha256") or "")
        if expected_digest and digest != expected_digest:
            raise BackupError(f"Stored object integrity check failed for {key}")
        archive_path = _archive_object_path(key, version_id)
        destination = root / archive_path
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination.write_bytes(data)
        os.chmod(destination, 0o600)
        objects[identity] = {
            "key": key,
            "source_version_id": version_id,
            "archive_path": archive_path,
            "content_type": content_type,
            "size": len(data),
            "sha256": digest,
            "references": [reference],
        }
    return list(objects.values())


def create_backup(
    destination: Path, settings: Settings, key: bytes
) -> dict[str, object]:
    _require_program("pg_dump")
    if destination.exists():
        raise BackupError(f"Refusing to overwrite existing archive: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    work_root = settings.data_dir / ".backup-work"
    work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(
        prefix="dayfinch-backup-", dir=work_root
    ) as temporary_name:
        temporary = Path(temporary_name)
        database_dump = temporary / "database.dump"
        with psycopg.connect(settings.database_url, row_factory=dict_row) as connection:
            connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            snapshot_id = connection.execute("SELECT pg_export_snapshot()").fetchone()[
                "pg_export_snapshot"
            ]
            rows = _object_rows(connection)
            _run(
                [
                    "pg_dump",
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                    f"--snapshot={snapshot_id}",
                    f"--file={database_dump}",
                ],
                settings.database_url,
            )
            connection.commit()
        objects = _snapshot_objects(rows, create_storage(settings), temporary)
        manifest: dict[str, object] = {
            "format": "dayfinch-encrypted-backup",
            "version": FORMAT_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "database_name": _database_name(settings.database_url),
            "database_dump": {
                "archive_path": "database.dump",
                "size": database_dump.stat().st_size,
                "sha256": _sha256(database_dump),
            },
            "source_storage_backend": settings.storage_backend,
            "objects": objects,
        }
        manifest_path = temporary / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.chmod(manifest_path, 0o600)
        tar_path = temporary / "payload.tar"
        with tarfile.open(tar_path, "w") as archive:
            archive.add(database_dump, arcname="database.dump", recursive=False)
            archive.add(manifest_path, arcname="manifest.json", recursive=False)
            for item in objects:
                archive.add(
                    temporary / str(item["archive_path"]),
                    arcname=str(item["archive_path"]),
                    recursive=False,
                )
        _encrypt(tar_path, destination, key)
        return manifest


def _safe_extract(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tarfile.open(source, "r") as archive:
        for member in archive.getmembers():
            member_path = PurePosixPath(member.name)
            if (
                member_path.is_absolute()
                or ".." in member_path.parts
                or not member.isfile()
            ):
                raise BackupError("Backup contains an unsafe archive member")
            target = destination.joinpath(*member_path.parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            stream = archive.extractfile(member)
            if stream is None:
                raise BackupError("Backup contains an unreadable archive member")
            with target.open("xb") as output:
                shutil.copyfileobj(stream, output, length=CHUNK_SIZE)
            os.chmod(target, 0o600)


def _load_and_verify_manifest(root: Path) -> dict[str, object]:
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError("Backup manifest is missing or invalid") from exc
    if (
        manifest.get("format") != "dayfinch-encrypted-backup"
        or manifest.get("version") != FORMAT_VERSION
    ):
        raise BackupError("Backup format version is unsupported")
    database_dump = manifest.get("database_dump")
    objects = manifest.get("objects")
    if not isinstance(database_dump, dict) or not isinstance(objects, list):
        raise BackupError("Backup manifest structure is invalid")
    items = [database_dump, *objects]
    for item in items:
        if not isinstance(item, dict):
            raise BackupError("Backup manifest object is invalid")
        relative = PurePosixPath(str(item.get("archive_path", "")))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise BackupError("Backup manifest contains an unsafe path")
        path = root.joinpath(*relative.parts)
        if not path.is_file():
            raise BackupError(f"Backup payload is missing {relative}")
        if path.stat().st_size != item.get("size") or _sha256(path) != item.get(
            "sha256"
        ):
            raise BackupError(f"Backup payload integrity check failed for {relative}")
    return manifest


def _restore_objects(
    root: Path,
    objects: list[dict[str, object]],
    storage: ScreenshotStore,
    database_url: str,
) -> None:
    version_updates: list[tuple[str, str, str | None]] = []
    for item in objects:
        key = str(item["key"])
        payload = root / str(item["archive_path"])
        stored = storage.save_blob(
            key,
            payload.read_bytes(),
            str(item.get("content_type") or "application/octet-stream"),
        )
        references = item.get("references")
        if not isinstance(references, list):
            raise BackupError("Backup object references are invalid")
        for reference in references:
            if not isinstance(reference, dict):
                raise BackupError("Backup object reference is invalid")
            version_updates.append(
                (
                    str(reference.get("table")),
                    str(reference.get("id")),
                    stored.version_id,
                )
            )
    with psycopg.connect(database_url) as connection:
        for table, record_id, version_id in version_updates:
            if table == "activity_records":
                result = connection.execute(
                    "UPDATE activity_records SET storage_version_id=%s WHERE id=%s",
                    (version_id, record_id),
                )
            elif table == "invoices":
                result = connection.execute(
                    "UPDATE invoices SET document_version_id=%s WHERE id=%s",
                    (version_id, record_id),
                )
            elif table == "team_invoices":
                result = connection.execute(
                    "UPDATE team_invoices SET document_version_id=%s WHERE id=%s",
                    (version_id, record_id),
                )
            else:
                raise BackupError("Backup contains an unsupported object reference")
            if result.rowcount != 1:
                raise BackupError(
                    f"Restored object reference was not found: {table}/{record_id}"
                )


def restore_backup(
    source: Path,
    settings: Settings,
    key: bytes,
    confirmed_database: str,
) -> dict[str, object]:
    _require_program("pg_restore")
    database_name = _database_name(settings.database_url)
    if confirmed_database != database_name:
        raise BackupError(
            "Restore confirmation does not exactly match the target database name"
        )
    if not source.is_file():
        raise BackupError(f"Backup archive was not found: {source}")
    work_root = settings.data_dir / ".backup-work"
    work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(
        prefix="dayfinch-restore-", dir=work_root
    ) as temporary_name:
        temporary = Path(temporary_name)
        tar_path = temporary / "payload.tar"
        _decrypt(source, tar_path, key)
        extracted = temporary / "extracted"
        _safe_extract(tar_path, extracted)
        manifest = _load_and_verify_manifest(extracted)
        _run(
            [
                "pg_restore",
                "--clean",
                "--if-exists",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                f"--dbname={database_name}",
                str(extracted / "database.dump"),
            ],
            settings.database_url,
        )
        objects = manifest["objects"]
        if not isinstance(objects, list):
            raise BackupError("Backup manifest objects are invalid")
        _restore_objects(
            extracted, objects, create_storage(settings), settings.database_url
        )
        return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dayfinch-ops",
        description="Encrypted, integrity-checked Dayfinch backup and restore",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("backup", help="create an encrypted backup")
    backup.add_argument("archive", type=Path)
    backup.add_argument(
        "--maintenance-confirmed",
        action="store_true",
        help="confirm writes are stopped for a consistent database/object snapshot",
    )
    restore = subparsers.add_parser("restore", help="restore an encrypted backup")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--confirm-database", required=True)
    restore.add_argument(
        "--maintenance-confirmed",
        action="store_true",
        help="confirm the application is stopped before destructive restore",
    )
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    if not arguments.maintenance_confirmed:
        raise SystemExit(
            "Refusing to proceed without --maintenance-confirmed; stop application writes first"
        )
    try:
        settings = Settings.from_env()
        settings.prepare()
        key = encryption_key()
        if arguments.command == "backup":
            manifest = create_backup(arguments.archive.resolve(), settings, key)
            print(
                f"Created {arguments.archive}: {len(manifest['objects'])} objects, "
                f"database {manifest['database_name']}"
            )
        else:
            manifest = restore_backup(
                arguments.archive.resolve(),
                settings,
                key,
                arguments.confirm_database,
            )
            print(
                f"Restored {arguments.archive}: {len(manifest['objects'])} objects, "
                f"database {manifest['database_name']}"
            )
    except (BackupError, OSError, psycopg.Error) as exc:
        print(f"dayfinch-ops: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

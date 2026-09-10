from __future__ import annotations

import struct
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath

MAX_ZIP32_VALUE = (1 << 32) - 1
UTF8_FLAG = 1 << 11


@dataclass(frozen=True)
class ZipEntry:
    name: str
    data: bytes
    modified_at: datetime


def _safe_name(value: str) -> bytes:
    path = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("ZIP entry name is unsafe")
    encoded = value.encode("utf-8")
    if len(encoded) > 65_535:
        raise ValueError("ZIP entry name is too long")
    return encoded


def _dos_time(value: datetime) -> tuple[int, int]:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    year = min(2107, max(1980, value.year))
    dos_date = ((year - 1980) << 9) | (value.month << 5) | value.day
    dos_time = (value.hour << 11) | (value.minute << 5) | (value.second // 2)
    return dos_time, dos_date


def stream_zip(entries: Iterable[ZipEntry]) -> Iterator[bytes]:
    """Write a stored ZIP32 archive without buffering file bodies or seeking."""
    central_directory: list[bytes] = []
    offset = 0
    seen: set[bytes] = set()
    for entry in entries:
        name = _safe_name(entry.name)
        if name in seen:
            raise ValueError("ZIP entry names must be unique")
        seen.add(name)
        size = len(entry.data)
        local_size = 30 + len(name) + size
        if (
            size > MAX_ZIP32_VALUE
            or offset > MAX_ZIP32_VALUE
            or offset + local_size > MAX_ZIP32_VALUE
            or len(central_directory) >= 65_535
        ):
            raise ValueError("ZIP32 archive limit exceeded")
        crc = zlib.crc32(entry.data) & 0xFFFFFFFF
        dos_time, dos_date = _dos_time(entry.modified_at)
        local_header = struct.pack(
            "<IHHHHHIIIHH",
            0x04034B50,
            20,
            UTF8_FLAG,
            0,
            dos_time,
            dos_date,
            crc,
            size,
            size,
            len(name),
            0,
        )
        central_directory.append(
            struct.pack(
                "<IHHHHHHIIIHHHHHII",
                0x02014B50,
                0x0314,
                20,
                UTF8_FLAG,
                0,
                dos_time,
                dos_date,
                crc,
                size,
                size,
                len(name),
                0,
                0,
                0,
                0,
                0o100600 << 16,
                offset,
            )
            + name
        )
        yield local_header
        yield name
        yield entry.data
        offset += len(local_header) + len(name) + size

    central_offset = offset
    for record in central_directory:
        yield record
        offset += len(record)
    central_size = offset - central_offset
    if max(offset, central_size, central_offset) > MAX_ZIP32_VALUE:
        raise ValueError("ZIP32 archive limit exceeded")
    yield struct.pack(
        "<IHHHHIIH",
        0x06054B50,
        0,
        0,
        len(central_directory),
        len(central_directory),
        central_size,
        central_offset,
        0,
    )

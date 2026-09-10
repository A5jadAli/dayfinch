from __future__ import annotations

import csv
import io
from collections.abc import Iterable


def safe_csv_cell(value: object) -> str:
    text = "" if value is None else str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def csv_bytes(fields: list[str], rows: Iterable[dict]) -> bytes:
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: safe_csv_cell(row.get(field)) for field in fields})
    return stream.getvalue().encode("utf-8")

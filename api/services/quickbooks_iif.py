from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

TIMEACT_COLUMNS = (
    "!TIMEACT",
    "DATE",
    "JOB",
    "EMP",
    "ITEM",
    "PITEM",
    "DURATION",
    "PROJ",
    "NOTE",
    "BILLINGSTATUS",
)
MAX_IIF_ROWS = 10_000
MAX_DURATION_MINUTES = 23 * 60 + 59
FORMULA_PREFIXES = ("=", "+", "-", "@")


class QuickBooksIIFError(ValueError):
    pass


@dataclass(frozen=True)
class QuickBooksIIFExport:
    data: bytes
    row_count: int
    source_seconds: int
    exported_minutes: int
    skipped_seconds: int


def validate_mapping(
    value: str,
    label: str,
    *,
    required: bool = False,
    max_length: int = 209,
) -> str:
    cleaned = value.strip()
    if required and not cleaned:
        raise QuickBooksIIFError(f"{label} is required")
    if len(cleaned) > max_length:
        raise QuickBooksIIFError(
            f"{label} must contain at most {max_length} characters"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in cleaned):
        raise QuickBooksIIFError(f"{label} cannot contain control characters")
    try:
        cleaned.encode("ascii")
    except UnicodeEncodeError as exc:
        raise QuickBooksIIFError(
            f"{label} must use ASCII characters for QuickBooks IIF"
        ) from exc
    if cleaned.startswith(FORMULA_PREFIXES):
        raise QuickBooksIIFError(
            f"{label} cannot begin with a spreadsheet formula character"
        )
    return cleaned


def _safe_note(value: Any) -> str:
    note = str(value or "").replace("\r\n", "/n").replace("\r", "/n")
    note = note.replace("\n", "/n").replace("\t", " ")
    note = "".join(
        character if ord(character) >= 32 and ord(character) != 127 else " "
        for character in note
    )
    note = "".join(
        character
        for character in unicodedata.normalize("NFKD", note)
        if not unicodedata.combining(character)
    )
    note = note.encode("ascii", "replace").decode()
    note = note[:1000]
    if note.startswith(FORMULA_PREFIXES):
        note = f"'{note}"[:1000]
    return note


def _work_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise QuickBooksIIFError("A time row has an invalid work date") from exc


def _row_values(
    row: dict[str, Any], default_service_item: str
) -> tuple[str, str, str, str, str, str, int, int]:
    employee = validate_mapping(
        str(row.get("quickbooks_name") or ""), "QuickBooks employee name", required=True
    )
    job = validate_mapping(
        str(row.get("quickbooks_customer_job") or ""), "QuickBooks customer/job"
    )
    service_item = validate_mapping(
        str(row.get("quickbooks_service_item") or default_service_item),
        "QuickBooks service item",
        required=True,
    )
    class_name = validate_mapping(
        str(row.get("quickbooks_class") or ""),
        "QuickBooks class",
        max_length=159,
    )
    billable = bool(row.get("quickbooks_billable"))
    if billable and not job:
        raise QuickBooksIIFError(
            "Billable time requires a QuickBooks customer/job mapping"
        )
    try:
        seconds = int(row.get("seconds", 0))
    except (TypeError, ValueError) as exc:
        raise QuickBooksIIFError("A time row has an invalid duration") from exc
    if seconds < 0:
        raise QuickBooksIIFError("A time row has a negative duration")
    rounded_minutes = (seconds + 30) // 60
    return (
        _work_date(row.get("work_date")).strftime("%m/%d/%y"),
        job,
        employee,
        service_item,
        class_name,
        _safe_note(row.get("note")),
        1 if billable else 0,
        rounded_minutes,
    )


def quickbooks_timer_iif(
    *,
    company_name: str,
    company_create_time: str,
    default_service_item: str,
    rows: list[dict[str, Any]],
    generated_at: datetime | None = None,
) -> QuickBooksIIFExport:
    company = validate_mapping(
        company_name, "QuickBooks company name", required=True, max_length=255
    )
    company_time = company_create_time.strip()
    if (
        not company_time.isascii()
        or not company_time.isdigit()
        or len(company_time) > 20
    ):
        raise QuickBooksIIFError(
            "QuickBooks company creation time must be the numeric value exported by QuickBooks"
        )
    default_item = validate_mapping(
        default_service_item, "Default QuickBooks service item"
    )
    observed = generated_at or datetime.now(UTC)
    observed = (
        observed.replace(tzinfo=UTC)
        if observed.tzinfo is None
        else observed.astimezone(UTC)
    )
    output: list[tuple[str, ...]] = [
        (
            "!TIMERHDR",
            "VER",
            "REL",
            "COMPANYNAME",
            "IMPORTEDBEFORE",
            "FROMTIMER",
            "COMPANYCREATETIME",
        ),
        ("TIMERHDR", "8", "0", company, "N", "Y", company_time),
        (
            "!HDR",
            "PROD",
            "VER",
            "REL",
            "IIFVER",
            "DATE",
            "TIME",
            "ACCNTNT",
            "ACCNTNTSPLITTIME",
        ),
        (
            "HDR",
            "Dayfinch Workforce Tracker",
            "Version 0.6.0",
            "Release 0",
            "1",
            observed.strftime("%m/%d/%Y"),
            str(int(observed.timestamp())),
            "N",
            "0",
        ),
        TIMEACT_COLUMNS,
    ]
    source_seconds = 0
    exported_minutes = 0
    skipped_seconds = 0
    row_count = 0
    for row in rows:
        values = _row_values(row, default_item)
        minutes = values[-1]
        seconds = int(row.get("seconds", 0))
        source_seconds += seconds
        if minutes == 0:
            skipped_seconds += seconds
            continue
        while minutes:
            duration = min(minutes, MAX_DURATION_MINUTES)
            hours, minute = divmod(duration, 60)
            output.append(
                (
                    "TIMEACT",
                    values[0],
                    values[1],
                    values[2],
                    values[3],
                    "",
                    f"{hours}:{minute:02d}",
                    values[4],
                    values[5],
                    str(values[6]),
                )
            )
            minutes -= duration
            exported_minutes += duration
            row_count += 1
            if row_count > MAX_IIF_ROWS:
                raise QuickBooksIIFError(
                    f"QuickBooks exports are limited to {MAX_IIF_ROWS} rows"
                )
    payload = "\r\n".join("\t".join(line) for line in output) + "\r\n"
    return QuickBooksIIFExport(
        data=payload.encode("ascii"),
        row_count=row_count,
        source_seconds=source_seconds,
        exported_minutes=exported_minutes,
        skipped_seconds=skipped_seconds,
    )

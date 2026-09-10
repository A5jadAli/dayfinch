from __future__ import annotations

import re
import threading
from datetime import UTC, datetime
from html import escape
from importlib.resources import files
from io import BytesIO
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_FONT_LOCK = threading.Lock()
_FONT_READY = False
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PDF_CAPACITY = threading.BoundedSemaphore(2)


class PDFExportBusy(RuntimeError):
    pass


def _fonts() -> tuple[str, str]:
    global _FONT_READY
    with _FONT_LOCK:
        if not _FONT_READY:
            font_dir = files("reportlab").joinpath("fonts")
            pdfmetrics.registerFont(
                TTFont("DayfinchSans", str(font_dir.joinpath("Vera.ttf")))
            )
            pdfmetrics.registerFont(
                TTFont("DayfinchSans-Bold", str(font_dir.joinpath("VeraBd.ttf")))
            )
            _FONT_READY = True
    return "DayfinchSans", "DayfinchSans-Bold"


def _safe_text(value: object) -> str:
    text = _CONTROL_CHARACTERS.sub("", str(value if value is not None else "—"))
    return escape(text[:2_000]).replace("\n", "<br/>")


def _display_value(field: str, value: object) -> str:
    if value in {None, ""}:
        return "—"
    if field == "seconds":
        seconds = max(0, int(value))
        return f"{seconds // 3600}h {seconds // 60 % 60:02d}m"
    if field == "activity_percent":
        return f"{max(0, min(100, int(value)))}%"
    if isinstance(value, datetime):
        normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value
        return normalized.isoformat(timespec="seconds")
    return str(value)


def _render_pdf_bytes(
    *,
    title: str,
    subtitle: str,
    fields: list[str],
    labels: dict[str, str],
    rows: list[dict[str, Any]],
    generated_at: datetime | None = None,
) -> bytes:
    if not fields:
        raise ValueError("A PDF report needs at least one column")
    regular_font, bold_font = _fonts()
    generated = generated_at or datetime.now(UTC)
    output = BytesIO()
    page_width, _ = landscape(A4)
    document = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        title=title[:200],
        author="Dayfinch",
        subject=subtitle[:500],
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        pageCompression=1,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "DayfinchTitle",
        parent=styles["Title"],
        fontName=bold_font,
        fontSize=17,
        leading=21,
        textColor=colors.HexColor("#172033"),
        spaceAfter=3 * mm,
    )
    meta_style = ParagraphStyle(
        "DayfinchMeta",
        parent=styles["Normal"],
        fontName=regular_font,
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#64748b"),
    )
    cell_style = ParagraphStyle(
        "DayfinchCell",
        parent=styles["Normal"],
        fontName=regular_font,
        fontSize=6.5,
        leading=8.5,
        textColor=colors.HexColor("#243044"),
        wordWrap="CJK",
    )
    header_style = ParagraphStyle(
        "DayfinchHeader",
        parent=cell_style,
        fontName=bold_font,
        textColor=colors.white,
    )

    data: list[list[Paragraph]] = [
        [
            Paragraph(_safe_text(labels.get(field, field)), header_style)
            for field in fields
        ]
    ]
    for row in rows:
        data.append(
            [
                Paragraph(_safe_text(_display_value(field, row.get(field))), cell_style)
                for field in fields
            ]
        )
    if not rows:
        data.append(
            [Paragraph("No rows match the selected filters.", cell_style)]
            + [Paragraph("", cell_style) for _ in fields[1:]]
        )

    available_width = page_width - document.leftMargin - document.rightMargin
    column_widths = [available_width / len(fields)] * len(fields)
    table = Table(data, colWidths=column_widths, repeatRows=1, splitByRow=1)
    commands: list[tuple] = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4f46e5")),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#dbe2ea")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for index in range(2, len(data), 2):
        commands.append(
            ("BACKGROUND", (0, index), (-1, index), colors.HexColor("#f8fafc"))
        )
    if not rows and len(fields) > 1:
        commands.append(("SPAN", (0, 1), (-1, 1)))
    table.setStyle(TableStyle(commands))

    footer_style = ParagraphStyle(
        "DayfinchFooter",
        parent=meta_style,
        alignment=TA_RIGHT,
    )

    def page_footer(canvas, _document) -> None:
        canvas.saveState()
        canvas.setTitle(title[:200])
        canvas.setAuthor("Dayfinch")
        footer = Paragraph(
            f"Generated {escape(generated.isoformat(timespec='seconds'))} · "
            f"Page {canvas.getPageNumber()}",
            footer_style,
        )
        footer.wrapOn(canvas, available_width, 8 * mm)
        footer.drawOn(canvas, document.leftMargin, 5 * mm)
        canvas.restoreState()

    story = [
        Paragraph(_safe_text(title), title_style),
        Paragraph(_safe_text(subtitle), meta_style),
        Spacer(1, 5 * mm),
        table,
    ]
    document.build(story, onFirstPage=page_footer, onLaterPages=page_footer)
    return output.getvalue()


def pdf_bytes(
    *,
    title: str,
    subtitle: str,
    fields: list[str],
    labels: dict[str, str],
    rows: list[dict[str, Any]],
    generated_at: datetime | None = None,
) -> bytes:
    if not _PDF_CAPACITY.acquire(timeout=1):
        raise PDFExportBusy("PDF rendering capacity is currently busy")
    try:
        return _render_pdf_bytes(
            title=title,
            subtitle=subtitle,
            fields=fields,
            labels=labels,
            rows=rows,
            generated_at=generated_at,
        )
    finally:
        _PDF_CAPACITY.release()

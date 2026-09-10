from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from api.services import pdf_export
from api.services.pdf_export import PDFExportBusy, pdf_bytes


def _render(title: str = "Dayfinch report") -> bytes:
    return pdf_bytes(
        title=title,
        subtitle="2026-09-01 through 2026-09-07 · Grouping: Project",
        fields=["project", "seconds", "activity_percent"],
        labels={
            "project": "Project",
            "seconds": "Duration",
            "activity_percent": "Activity %",
        },
        rows=[
            {
                "project": "Research & development <phase 2>",
                "seconds": 5_430,
                "activity_percent": 73,
            },
            {
                "project": "Control\x00characters\nwrap safely",
                "seconds": 0,
                "activity_percent": 200,
            },
        ],
        generated_at=datetime(2026, 9, 7, 12, tzinfo=UTC),
    )


def test_pdf_export_is_a_complete_bounded_document():
    payload = _render()

    assert payload.startswith(b"%PDF-")
    assert payload.rstrip().endswith(b"%%EOF")
    assert 1_000 < len(payload) < 1_000_000


def test_pdf_export_supports_empty_results_and_requires_columns():
    empty = pdf_bytes(
        title="Empty report",
        subtitle="No matching rows",
        fields=["project"],
        labels={"project": "Project"},
        rows=[],
    )
    assert empty.startswith(b"%PDF-")

    with pytest.raises(ValueError, match="at least one column"):
        pdf_bytes(title="Invalid", subtitle="", fields=[], labels={}, rows=[])


def test_pdf_font_registration_is_thread_safe():
    with ThreadPoolExecutor(max_workers=8) as executor:
        documents = list(
            executor.map(_render, [f"Report {index}" for index in range(16)])
        )

    assert len(documents) == 16
    assert all(document.startswith(b"%PDF-") for document in documents)


def test_pdf_rendering_fails_fast_when_capacity_is_saturated(monkeypatch):
    class Saturated:
        @staticmethod
        def acquire(timeout):
            assert timeout == 1
            return False

        @staticmethod
        def release():
            raise AssertionError("an unacquired slot must not be released")

    monkeypatch.setattr(pdf_export, "_PDF_CAPACITY", Saturated())

    with pytest.raises(PDFExportBusy, match="capacity"):
        _render()

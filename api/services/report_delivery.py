from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import UTC, datetime, time
from email.message import EmailMessage
from uuid import uuid4

from ..config import Settings
from ..database import Database
from .csv_export import csv_bytes
from .pdf_export import PDFExportBusy, pdf_bytes
from .report_schedule import (
    next_report_delivery,
    scheduled_report_retry_at,
    scheduled_report_window,
)

MAX_SCHEDULED_PDF_ROWS = 1_000
MAX_SCHEDULED_CSV_ROWS = 10_000
LOGGER = logging.getLogger("dayfinch-report-delivery")


class ScheduledReportTooLarge(ValueError):
    pass


class ScheduledReportConfigurationError(ValueError):
    pass


class ReportDeliveryService:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings

    def _rows(
        self,
        report_type: str,
        started_at: datetime,
        ended_at: datetime,
        limit: int,
    ) -> list[dict]:
        if report_type == "time":
            return self.database.time_report(
                started_at=started_at, ended_at=ended_at, limit=limit
            )
        if report_type == "activity":
            return self.database.activity_report(
                started_at=started_at, ended_at=ended_at, limit=limit
            )
        if report_type == "attendance":
            return self.database.attendance_report(
                started_at=started_at, ended_at=ended_at, limit=limit
            )
        if report_type == "expenses":
            return self.database.list_expenses(
                incurred_from=started_at.date(),
                incurred_to=ended_at.date(),
                limit=limit,
            )
        raise ScheduledReportConfigurationError("Unsupported scheduled report type")

    @staticmethod
    def _fields(rows: list[dict]) -> tuple[list[str], list[dict]]:
        fields = list(dict.fromkeys(key for row in rows for key in row))
        if not fields:
            fields = ["message"]
            rows = [{"message": "No records in this report period"}]
        return fields, rows

    @classmethod
    def _csv(cls, rows: list[dict]) -> bytes:
        fields, rows = cls._fields(rows)
        return csv_bytes(fields, rows)

    @classmethod
    def _pdf(cls, report_name: str, report_type: str, rows: list[dict]) -> bytes:
        if len(rows) > MAX_SCHEDULED_PDF_ROWS:
            raise ScheduledReportTooLarge(
                f"Scheduled PDF exceeds {MAX_SCHEDULED_PDF_ROWS} rows; use CSV"
            )
        fields, rows = cls._fields(rows)
        return pdf_bytes(
            title=f"Dayfinch scheduled report: {report_name}",
            subtitle=f"Report type: {report_type.replace('_', ' ').title()}",
            fields=fields,
            labels={field: field.replace("_", " ").title() for field in fields},
            rows=rows,
        )

    @staticmethod
    def _recipients(value: str) -> list[str]:
        recipients = [
            item.strip() for item in value.replace(";", ",").split(",") if item.strip()
        ]
        if not recipients or any(
            item.count("@") != 1
            or any(character in item for character in "\r\n")
            or len(item) > 320
            for item in recipients
        ):
            raise ScheduledReportConfigurationError("Invalid report recipients")
        return recipients

    def _deliver_one(self, report: dict, sent_at: datetime, claim_token: str) -> None:
        recipients = self._recipients(report["recipients"])
        delivery_format = report.get("delivery_format", "csv")
        if delivery_format not in {"csv", "pdf"}:
            raise ScheduledReportConfigurationError("Invalid report format")
        started_at, ended_at = scheduled_report_window(
            sent_at,
            report["frequency"],
            report.get("range_preset", "previous_period"),
        )
        row_limit = (
            MAX_SCHEDULED_PDF_ROWS + 1
            if delivery_format == "pdf"
            else MAX_SCHEDULED_CSV_ROWS + 1
        )
        rows = self._rows(report["report_type"], started_at, ended_at, row_limit)
        if delivery_format == "csv" and len(rows) > MAX_SCHEDULED_CSV_ROWS:
            raise ScheduledReportTooLarge(
                f"Scheduled CSV exceeds {MAX_SCHEDULED_CSV_ROWS} rows"
            )

        message = EmailMessage()
        message["Subject"] = f"Dayfinch report: {report['name']}"
        message["From"] = self.settings.smtp_from_email
        message["To"] = ", ".join(recipients)
        message.set_content(
            f"Your scheduled Dayfinch {report['report_type']} report for "
            f"{started_at.date().isoformat()} through "
            f"{(ended_at.date()).isoformat()} (exclusive) is attached as "
            f"{delivery_format.upper()}."
        )
        if delivery_format == "pdf":
            message.add_attachment(
                self._pdf(report["name"], report["report_type"], rows),
                maintype="application",
                subtype="pdf",
                filename=f"dayfinch-{report['report_type']}.pdf",
            )
        else:
            message.add_attachment(
                self._csv(rows),
                maintype="text",
                subtype="csv",
                filename=f"dayfinch-{report['report_type']}.csv",
            )
        with smtplib.SMTP(
            self.settings.smtp_host,
            self.settings.smtp_port,
            timeout=self.settings.smtp_timeout_seconds,
        ) as smtp:
            if self.settings.smtp_starttls:
                smtp.starttls(context=ssl.create_default_context())
            if self.settings.smtp_username:
                smtp.login(self.settings.smtp_username, self.settings.smtp_password)
            smtp.send_message(message)

        next_send_at = next_report_delivery(
            sent_at,
            report["frequency"],
            time(report["delivery_hour"], report["delivery_minute"]),
            weekday=report["schedule_weekday"],
            month_day=report["schedule_month_day"],
        )
        self.database.mark_scheduled_report_sent(
            report["id"], sent_at, next_send_at, claim_token
        )

    @staticmethod
    def _error_code(exc: Exception) -> str:
        if isinstance(exc, ScheduledReportTooLarge):
            return "report_too_large"
        if isinstance(exc, ScheduledReportConfigurationError):
            return "invalid_configuration"
        if isinstance(exc, PDFExportBusy):
            return "pdf_capacity"
        if isinstance(exc, (smtplib.SMTPException, OSError)):
            return "delivery_unavailable"
        return "generation_failed"

    def deliver_due(self, now: datetime | None = None) -> int:
        if not self.settings.smtp_host or not self.settings.smtp_from_email:
            return 0
        observed = now or datetime.now(UTC)
        sent_at = (
            observed.replace(tzinfo=UTC)
            if observed.tzinfo is None
            else observed.astimezone(UTC)
        )
        sent = 0
        claim_token = str(uuid4())
        for report in self.database.due_scheduled_reports(sent_at, claim_token):
            try:
                self._deliver_one(report, sent_at, claim_token)
            except Exception as exc:  # noqa: BLE001 - isolate individual schedules
                error_code = self._error_code(exc)
                next_attempt_at = scheduled_report_retry_at(
                    sent_at,
                    int(report.get("consecutive_failures", 0)),
                    report["id"],
                )
                self.database.mark_scheduled_report_failed(
                    report["id"], sent_at, next_attempt_at, error_code, claim_token
                )
                LOGGER.warning(
                    "scheduled_report_delivery_failed",
                    extra={
                        "report_type": report.get("report_type", "unknown"),
                        "error_code": error_code,
                        "failure_count": int(report.get("consecutive_failures", 0)) + 1,
                    },
                )
                continue
            sent += 1
        return sent

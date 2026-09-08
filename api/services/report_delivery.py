from __future__ import annotations

import asyncio
import csv
import io
import logging
import smtplib
from datetime import UTC, datetime
from email.message import EmailMessage

from ..config import Settings
from ..database import Database

LOGGER = logging.getLogger("dayfinch-report-delivery")


class ReportDeliveryService:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings

    def _rows(self, report_type: str) -> list[dict]:
        if report_type == "time":
            return self.database.time_report()
        if report_type == "activity":
            return self.database.activity_report()
        if report_type == "attendance":
            return self.database.attendance_report()
        if report_type == "expenses":
            return self.database.list_expenses()
        raise ValueError(f"Unsupported scheduled report: {report_type}")

    @staticmethod
    def _csv(rows: list[dict]) -> bytes:
        fields = list(dict.fromkeys(key for row in rows for key in row))
        if not fields:
            fields = ["message"]
            rows = [{"message": "No records in this report period"}]
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return stream.getvalue().encode("utf-8")

    def deliver_due(self, now: datetime | None = None) -> int:
        if not self.settings.smtp_host or not self.settings.smtp_from_email:
            return 0
        sent_at = now or datetime.now(UTC)
        sent = 0
        for report in self.database.due_scheduled_reports(sent_at):
            recipients = [
                value.strip()
                for value in report["recipients"].replace(";", ",").split(",")
                if "@" in value
            ]
            if not recipients:
                continue
            message = EmailMessage()
            message["Subject"] = f"Dayfinch report: {report['name']}"
            message["From"] = self.settings.smtp_from_email
            message["To"] = ", ".join(recipients)
            message.set_content(
                f"Your scheduled Dayfinch {report['report_type']} report is attached."
            )
            message.add_attachment(
                self._csv(self._rows(report["report_type"])),
                maintype="text",
                subtype="csv",
                filename=f"dayfinch-{report['report_type']}.csv",
            )
            with smtplib.SMTP(
                self.settings.smtp_host, self.settings.smtp_port, timeout=30
            ) as smtp:
                if self.settings.smtp_starttls:
                    smtp.starttls()
                if self.settings.smtp_username:
                    smtp.login(self.settings.smtp_username, self.settings.smtp_password)
                smtp.send_message(message)
            self.database.mark_scheduled_report_sent(
                report["id"], report["frequency"], sent_at
            )
            sent += 1
        return sent


async def run_report_delivery_worker(service: ReportDeliveryService) -> None:
    while True:
        try:
            await asyncio.to_thread(service.deliver_due)
        except (OSError, smtplib.SMTPException, ValueError):
            LOGGER.exception("Scheduled report delivery failed; it will be retried")
        await asyncio.sleep(3600)

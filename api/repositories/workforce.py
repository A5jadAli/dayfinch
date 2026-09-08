from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from .base import RepositoryMixin, token_hash, utc_now


class WorkforceRepository(RepositoryMixin):
    """Queries and commands for the workforce-management side of Dayfinch."""

    def dashboard_summary(self, user_id: str | None = None) -> dict[str, Any]:
        user_clause = "AND ws.user_id = %s" if user_id else ""
        params: tuple[Any, ...] = (user_id,) if user_id else ()
        with self.connect() as connection:
            totals = connection.execute(
                f"""SELECT
                    COALESCE(SUM(EXTRACT(EPOCH FROM (COALESCE(s.ended_at, CURRENT_TIMESTAMP) - s.started_at))), 0)::BIGINT tracked_seconds,
                    COUNT(DISTINCT CASE WHEN ws.status = 'active' THEN ws.user_id END) active_members,
                    COUNT(DISTINCT ws.user_id) tracked_members
                  FROM work_session_segments s
                  JOIN work_sessions ws ON ws.id = s.session_id
                  WHERE s.started_at >= date_trunc('week', CURRENT_TIMESTAMP) {user_clause}""",
                params,
            ).fetchone()
            activity = connection.execute(
                f"""SELECT COALESCE(ROUND(AVG(a.activity_percent)), 0)::INTEGER activity_percent,
                           COUNT(*) screenshot_count,
                           COUNT(*) FILTER (WHERE a.automation_suspected) anomaly_count
                    FROM activity_records a
                    WHERE a.captured_at >= date_trunc('week', CURRENT_TIMESTAMP)
                      {("AND a.user_id = %s" if user_id else "")}""",
                params,
            ).fetchone()
            pending = connection.execute(
                """SELECT
                     (SELECT COUNT(*) FROM timesheets WHERE status='submitted') timesheets,
                     (SELECT COUNT(*) FROM manual_time_entries WHERE status='pending') manual_time,
                     (SELECT COUNT(*) FROM time_off_requests WHERE status='pending') time_off,
                     (SELECT COUNT(*) FROM expenses WHERE status='pending') expenses"""
            ).fetchone()
            projects = connection.execute(
                """SELECT p.id, p.name, p.color, p.budget_amount, p.budget_minutes,
                          p.budget_type, COUNT(DISTINCT pm.user_id) member_count,
                          COALESCE(SUM(EXTRACT(EPOCH FROM (COALESCE(seg.ended_at, CURRENT_TIMESTAMP)-seg.started_at))),0)::BIGINT tracked_seconds
                   FROM projects p
                   LEFT JOIN project_members pm ON pm.project_id=p.id
                   LEFT JOIN work_sessions ws ON ws.project_id=p.id
                   LEFT JOIN work_session_segments seg ON seg.session_id=ws.id AND seg.started_at >= date_trunc('week', CURRENT_TIMESTAMP)
                   WHERE p.enabled=TRUE GROUP BY p.id ORDER BY tracked_seconds DESC LIMIT 6"""
            ).fetchall()
            recent = connection.execute(
                """SELECT ws.id, u.email, u.full_name, p.name project_name, t.name task_name,
                          ws.status, ws.started_at,
                          COALESCE(SUM(EXTRACT(EPOCH FROM (COALESCE(seg.ended_at,CURRENT_TIMESTAMP)-seg.started_at))),0)::BIGINT tracked_seconds
                   FROM work_sessions ws LEFT JOIN users u ON u.id=ws.user_id
                   LEFT JOIN projects p ON p.id=ws.project_id LEFT JOIN tasks t ON t.id=ws.task_id
                   LEFT JOIN work_session_segments seg ON seg.session_id=ws.id
                   GROUP BY ws.id,u.id,p.id,t.id ORDER BY ws.started_at DESC LIMIT 8"""
            ).fetchall()
        return {
            **dict(totals),
            **dict(activity),
            "pending": dict(pending),
            "projects": [dict(row) for row in projects],
            "recent": [dict(row) for row in recent],
        }

    def activity_feed(
        self,
        user_id: str | None = None,
        project_id: str | None = None,
        limit: int = 120,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if user_id:
            conditions.append("a.user_id=%s")
            params.append(user_id)
        if project_id:
            conditions.append("a.project_id=%s")
            params.append(project_id)
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        params.append(min(max(limit, 1), 500))
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT a.*,u.email,u.full_name,p.name project_name,p.color project_color,
                           t.name task_name,d.name device_name
                    FROM activity_records a LEFT JOIN users u ON u.id=a.user_id
                    LEFT JOIN projects p ON p.id=a.project_id LEFT JOIN tasks t ON t.id=a.task_id
                    JOIN devices d ON d.id=a.device_id {where}
                    ORDER BY a.captured_at DESC LIMIT %s""",
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    def usage_summary(
        self, column: str, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        if column not in {"active_app", "active_url"}:
            raise ValueError("Unsupported usage dimension")
        where = "WHERE sample.name IS NOT NULL AND sample.name <> ''"
        params: tuple[Any, ...] = ()
        if user_id:
            where += " AND sample.user_id=%s"
            params = (user_id,)
        with self.connect() as connection:
            rows = connection.execute(
                f"""WITH sample AS (
                      SELECT u.{column} name,u.focused_seconds,u.user_id
                      FROM usage_records u
                      UNION ALL
                      SELECT a.{column} name,a.focused_seconds,a.user_id
                      FROM activity_records a
                      WHERE NOT EXISTS (
                        SELECT 1 FROM usage_records u
                        WHERE u.device_id=a.device_id AND u.observed_at::date=a.captured_at::date
                      )
                    )
                    SELECT sample.name,COUNT(*) sessions,
                           COALESCE(SUM(sample.focused_seconds),0)::BIGINT seconds,
                           COUNT(DISTINCT sample.user_id) members
                    FROM sample {where} GROUP BY sample.name
                    ORDER BY seconds DESC, name LIMIT 100""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def add_manual_time(
        self,
        user_id: str,
        project_id: str,
        task_id: str | None,
        started_at: datetime,
        ended_at: datetime,
        note: str,
        auto_approve: bool = False,
    ) -> str:
        if ended_at <= started_at:
            raise ValueError("End time must be after start time")
        entry_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO manual_time_entries(id,user_id,project_id,task_id,started_at,ended_at,note,status,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    entry_id,
                    user_id,
                    project_id,
                    task_id or None,
                    started_at,
                    ended_at,
                    note.strip()[:500],
                    "approved" if auto_approve else "pending",
                    utc_now(),
                ),
            )
        return entry_id

    def list_manual_time(self, user_id: str | None = None) -> list[dict[str, Any]]:
        where, params = ("WHERE m.user_id=%s", (user_id,)) if user_id else ("", ())
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT m.*,u.email,u.full_name,p.name project_name,t.name task_name,
                           EXTRACT(EPOCH FROM (m.ended_at-m.started_at))::BIGINT seconds
                    FROM manual_time_entries m JOIN users u ON u.id=m.user_id
                    JOIN projects p ON p.id=m.project_id LEFT JOIN tasks t ON t.id=m.task_id
                    {where} ORDER BY m.started_at DESC""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def review_item(
        self, table: str, item_id: str, reviewer_id: str, status: str
    ) -> None:
        allowed = {
            "manual_time_entries": {"approved", "rejected"},
            "time_off_requests": {"approved", "denied"},
            "expenses": {"approved", "rejected", "reimbursed"},
        }
        if table not in allowed or status not in allowed[table]:
            raise ValueError("Unsupported review decision")
        with self.connect() as connection:
            item = connection.execute(
                f"SELECT user_id FROM {table} WHERE id=%s", (item_id,)
            ).fetchone()
            result = connection.execute(
                f"UPDATE {table} SET status=%s,reviewed_by_user_id=%s,reviewed_at=%s WHERE id=%s",
                (status, reviewer_id, utc_now(), item_id),
            )
            if result.rowcount != 1:
                raise ValueError("Item not found")
            connection.execute(
                """INSERT INTO notifications(id,user_id,kind,title,body,created_at)
                   VALUES (%s,%s,'approval',%s,%s,%s)""",
                (
                    str(uuid.uuid4()),
                    item["user_id"],
                    f"{table.replace('_', ' ').title()} {status}",
                    "Your request was reviewed by a manager.",
                    utc_now(),
                ),
            )

    def add_shift(
        self,
        user_id: str,
        project_id: str | None,
        starts_at: datetime,
        ends_at: datetime,
        notes: str,
        creator_id: str,
    ) -> str:
        if ends_at <= starts_at:
            raise ValueError("Shift must end after it starts")
        item_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO shifts(id,user_id,project_id,starts_at,ends_at,notes,created_by_user_id,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    item_id,
                    user_id,
                    project_id or None,
                    starts_at,
                    ends_at,
                    notes.strip()[:500],
                    creator_id,
                    utc_now(),
                ),
            )
        return item_id

    def list_shifts(self, user_id: str | None = None) -> list[dict[str, Any]]:
        where, params = ("WHERE s.user_id=%s", (user_id,)) if user_id else ("", ())
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT s.*,u.email,u.full_name,p.name project_name
                    FROM shifts s JOIN users u ON u.id=s.user_id LEFT JOIN projects p ON p.id=s.project_id
                    {where} ORDER BY s.starts_at DESC LIMIT 200""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def create_holiday(self, name: str, holiday_date: date, paid_minutes: int) -> str:
        holiday_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO holidays(id,name,holiday_date,paid_minutes,created_at)
                   VALUES (%s,%s,%s,%s,%s)""",
                (
                    holiday_id,
                    name.strip()[:120],
                    holiday_date,
                    max(0, paid_minutes),
                    utc_now(),
                ),
            )
        return holiday_id

    def list_holidays(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM holidays ORDER BY holiday_date DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def start_break(self, user_id: str, session_id: str, paid: bool = False) -> str:
        break_id = str(uuid.uuid4())
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM work_breaks WHERE user_id=%s AND ended_at IS NULL",
                (user_id,),
            ).fetchone()
            if existing:
                return existing["id"]
            connection.execute(
                """INSERT INTO work_breaks(id,user_id,session_id,started_at,paid)
                   VALUES (%s,%s,%s,%s,%s)""",
                (break_id, user_id, session_id, utc_now(), paid),
            )
        return break_id

    def stop_break(self, user_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE work_breaks SET ended_at=%s
                   WHERE user_id=%s AND ended_at IS NULL""",
                (utc_now(), user_id),
            )

    def add_time_off(
        self,
        user_id: str,
        category: str,
        starts_on: date,
        ends_on: date,
        minutes: int,
        reason: str,
    ) -> str:
        if ends_on < starts_on:
            raise ValueError("End date must be on or after start date")
        item_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO time_off_requests(id,user_id,category,starts_on,ends_on,minutes,reason,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    item_id,
                    user_id,
                    category[:40],
                    starts_on,
                    ends_on,
                    max(0, minutes),
                    reason.strip()[:500],
                    utc_now(),
                ),
            )
        return item_id

    def list_time_off(self, user_id: str | None = None) -> list[dict[str, Any]]:
        where, params = ("WHERE r.user_id=%s", (user_id,)) if user_id else ("", ())
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT r.*,u.email,u.full_name FROM time_off_requests r JOIN users u ON u.id=r.user_id {where} ORDER BY r.starts_on DESC",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def add_expense(
        self,
        user_id: str,
        project_id: str | None,
        incurred_on: date,
        category: str,
        amount: Decimal,
        currency: str,
        description: str,
    ) -> str:
        if amount < 0:
            raise ValueError("Amount cannot be negative")
        item_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO expenses(id,user_id,project_id,incurred_on,category,amount,currency,description,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    item_id,
                    user_id,
                    project_id or None,
                    incurred_on,
                    category[:60],
                    amount,
                    currency[:3].upper(),
                    description.strip()[:500],
                    utc_now(),
                ),
            )
        return item_id

    def list_expenses(self, user_id: str | None = None) -> list[dict[str, Any]]:
        where, params = ("WHERE e.user_id=%s", (user_id,)) if user_id else ("", ())
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT e.*,u.email,u.full_name,p.name project_name FROM expenses e
                    JOIN users u ON u.id=e.user_id LEFT JOIN projects p ON p.id=e.project_id
                    {where} ORDER BY e.incurred_on DESC""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def organization_settings(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM organization_settings WHERE id=1"
            ).fetchone()
        return dict(row)

    def update_organization_settings(self, values: dict[str, Any]) -> None:
        fields = (
            "name",
            "address",
            "tax_id",
            "timezone",
            "currency",
            "screenshot_frequency",
            "screenshot_blur",
            "track_apps",
            "track_urls",
            "allow_manual_time",
            "require_time_approval",
            "allow_screenshot_delete",
            "require_edit_reason",
            "allow_keep_idle",
            "pay_period",
            "require_two_factor",
            "sso_provider",
            "sso_domain",
            "idle_timeout_minutes",
            "retention_days",
        )
        assignments = ",".join(f"{field}=%s" for field in fields)
        with self.connect() as connection:
            current = connection.execute(
                "SELECT * FROM organization_settings WHERE id=1"
            ).fetchone()
            connection.execute(
                f"UPDATE organization_settings SET {assignments},updated_at=%s WHERE id=1",
                tuple(values.get(field, current[field]) for field in fields)
                + (utc_now(),),
            )

    def set_user_profile(
        self,
        user_id: str,
        role: str,
        full_name: str,
        pay_rate: Decimal,
        bill_rate: Decimal,
        weekly_limit_minutes: int,
        daily_limit_minutes: int | None = None,
    ) -> None:
        if role not in {"admin", "manager", "member", "viewer"}:
            raise ValueError("Invalid role")
        with self.connect() as connection:
            connection.execute(
                """UPDATE users SET role=%s,full_name=%s,pay_rate=%s,bill_rate=%s,weekly_limit_minutes=%s,
                   daily_limit_minutes=COALESCE(%s,daily_limit_minutes) WHERE id=%s""",
                (
                    role,
                    full_name.strip()[:120],
                    pay_rate,
                    bill_rate,
                    max(0, weekly_limit_minutes),
                    max(0, daily_limit_minutes)
                    if daily_limit_minutes is not None
                    else None,
                    user_id,
                ),
            )

    def finance_summary(self) -> dict[str, Any]:
        with self.connect() as connection:
            totals = connection.execute(
                """SELECT
                  COALESCE((SELECT SUM(amount) FROM expenses WHERE status IN ('approved','reimbursed')),0) expenses,
                  COALESCE((SELECT SUM(gross_amount) FROM payroll_payments WHERE status='paid'),0) payroll,
                  COALESCE((SELECT SUM(il.quantity*il.unit_price) FROM invoice_lines il JOIN invoices i ON i.id=il.invoice_id WHERE i.status='paid'),0) received,
                  COALESCE((SELECT SUM(il.quantity*il.unit_price) FROM invoice_lines il JOIN invoices i ON i.id=il.invoice_id WHERE i.status IN ('sent','overdue')),0) outstanding"""
            ).fetchone()
            invoices = connection.execute(
                """SELECT i.*,c.name client_name,COALESCE(SUM(il.quantity*il.unit_price),0) subtotal
                   FROM invoices i LEFT JOIN clients c ON c.id=i.client_id LEFT JOIN invoice_lines il ON il.invoice_id=i.id
                   GROUP BY i.id,c.id ORDER BY i.issued_on DESC"""
            ).fetchall()
            payroll = connection.execute(
                """SELECT pp.*,u.email,u.full_name FROM payroll_payments pp JOIN users u ON u.id=pp.user_id ORDER BY pp.period_start DESC"""
            ).fetchall()
            clients = connection.execute(
                "SELECT * FROM clients ORDER BY lower(name)"
            ).fetchall()
        return {
            **dict(totals),
            "invoices": [dict(x) for x in invoices],
            "payroll": [dict(x) for x in payroll],
            "clients": [dict(x) for x in clients],
        }

    def create_client(self, name: str, email: str, address: str = "") -> str:
        client_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO clients(id,name,email,address,created_at)
                   VALUES (%s,%s,%s,%s,%s)""",
                (
                    client_id,
                    name.strip()[:120],
                    email.strip()[:254],
                    address.strip()[:500],
                    utc_now(),
                ),
            )
        return client_id

    def create_invoice(
        self,
        client_id: str,
        issued_on: date,
        due_on: date,
        description: str,
        quantity: Decimal,
        unit_price: Decimal,
        currency: str,
        creator_id: str,
    ) -> str:
        if due_on < issued_on or quantity <= 0 or unit_price < 0:
            raise ValueError("Invoice dates, quantity, or rate are invalid")
        invoice_id, line_id = str(uuid.uuid4()), str(uuid.uuid4())
        with self.connect() as connection:
            sequence = connection.execute(
                "SELECT COUNT(*) + 1 value FROM invoices WHERE issued_on >= date_trunc('year', %s::date)",
                (issued_on,),
            ).fetchone()["value"]
            number = f"DF-{issued_on.year}-{sequence:04d}"
            connection.execute(
                """INSERT INTO invoices(id,number,client_id,issued_on,due_on,currency,created_by_user_id,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    invoice_id,
                    number,
                    client_id,
                    issued_on,
                    due_on,
                    currency[:3].upper(),
                    creator_id,
                    utc_now(),
                ),
            )
            connection.execute(
                """INSERT INTO invoice_lines(id,invoice_id,description,quantity,unit_price)
                   VALUES (%s,%s,%s,%s,%s)""",
                (line_id, invoice_id, description.strip()[:500], quantity, unit_price),
            )
        return invoice_id

    def invoice_snapshot(self, invoice_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            invoice = connection.execute(
                """SELECT i.*,c.name client_name,c.email client_email,
                          c.address client_address
                   FROM invoices i LEFT JOIN clients c ON c.id=i.client_id
                   WHERE i.id=%s""",
                (invoice_id,),
            ).fetchone()
            if not invoice:
                return None
            lines = connection.execute(
                """SELECT il.*,p.name project_name FROM invoice_lines il
                   LEFT JOIN projects p ON p.id=il.project_id
                   WHERE il.invoice_id=%s ORDER BY il.id""",
                (invoice_id,),
            ).fetchall()
        result = dict(invoice)
        result["lines"] = [dict(line) for line in lines]
        result["subtotal"] = sum(
            (line["quantity"] * line["unit_price"] for line in lines), Decimal(0)
        )
        return result

    def record_invoice_document(
        self,
        invoice_id: str,
        key: str,
        version_id: str | None,
        sha256_digest: str,
    ) -> None:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE invoices SET encrypted_document_key=%s,
                   document_version_id=%s,encrypted_document_sha256=%s,
                   document_sealed_at=%s WHERE id=%s""",
                (key, version_id, sha256_digest, utc_now(), invoice_id),
            )
            if result.rowcount != 1:
                raise ValueError("Invoice not found")

    def create_payroll(
        self, user_id: str, period_start: date, period_end: date, currency: str
    ) -> str:
        if period_end < period_start:
            raise ValueError("Payroll period is invalid")
        payment_id = str(uuid.uuid4())
        with self.connect() as connection:
            user = connection.execute(
                "SELECT pay_rate FROM users WHERE id=%s", (user_id,)
            ).fetchone()
            if not user:
                raise ValueError("Member not found")
            tracked = connection.execute(
                """SELECT COALESCE(SUM(EXTRACT(EPOCH FROM (COALESCE(seg.ended_at,CURRENT_TIMESTAMP)-seg.started_at))),0)/60 minutes
                   FROM work_sessions ws JOIN work_session_segments seg ON seg.session_id=ws.id
                   WHERE ws.user_id=%s AND seg.started_at::date BETWEEN %s AND %s""",
                (user_id, period_start, period_end),
            ).fetchone()["minutes"]
            manual = connection.execute(
                """SELECT COALESCE(SUM(EXTRACT(EPOCH FROM (ended_at-started_at))),0)/60 minutes
                   FROM manual_time_entries WHERE user_id=%s AND status='approved' AND started_at::date BETWEEN %s AND %s""",
                (user_id, period_start, period_end),
            ).fetchone()["minutes"]
            total_minutes = int((tracked or 0) + (manual or 0))
            regular = min(total_minutes, 2400)
            overtime = max(0, total_minutes - regular)
            gross = (Decimal(regular) / 60 * user["pay_rate"]) + (
                Decimal(overtime) / 60 * user["pay_rate"] * Decimal("1.5")
            )
            connection.execute(
                """INSERT INTO payroll_payments(id,user_id,period_start,period_end,regular_minutes,overtime_minutes,gross_amount,currency,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    payment_id,
                    user_id,
                    period_start,
                    period_end,
                    regular,
                    overtime,
                    gross.quantize(Decimal("0.01")),
                    currency[:3].upper(),
                    utc_now(),
                ),
            )
        return payment_id

    def set_financial_status(self, table: str, item_id: str, item_status: str) -> None:
        allowed = {
            "invoices": {"draft", "sent", "paid", "void", "overdue"},
            "payroll_payments": {"draft", "processing", "paid", "failed"},
        }
        if table not in allowed or item_status not in allowed[table]:
            raise ValueError("Invalid financial status")
        paid_field = (
            ",paid_at=CURRENT_TIMESTAMP"
            if table == "payroll_payments" and item_status == "paid"
            else ""
        )
        with self.connect() as connection:
            updated_field = (
                ",updated_at=CURRENT_TIMESTAMP" if table == "payroll_payments" else ""
            )
            result = connection.execute(
                f"UPDATE {table} SET status=%s{paid_field}{updated_field} WHERE id=%s",
                (item_status, item_id),
            )
            if result.rowcount != 1:
                raise ValueError("Financial record not found")

    def get_payroll_payment(self, payment_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT pp.*,u.email,u.full_name FROM payroll_payments pp
                   JOIN users u ON u.id=pp.user_id WHERE pp.id=%s""",
                (payment_id,),
            ).fetchone()
        return dict(row) if row else None

    def record_payroll_delivery(
        self,
        payment_id: str,
        status: str,
        *,
        provider: str = "webhook",
        external_reference: str = "",
        failure_reason: str = "",
    ) -> None:
        if status not in {"processing", "paid", "failed"}:
            raise ValueError("Invalid payroll delivery status")
        paid_at = utc_now() if status == "paid" else None
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE payroll_payments
                   SET status=%s,provider=%s,external_reference=%s,failure_reason=%s,
                       paid_at=%s,updated_at=%s WHERE id=%s""",
                (
                    status,
                    provider[:80],
                    external_reference[:255],
                    failure_reason[:1000],
                    paid_at,
                    utc_now(),
                    payment_id,
                ),
            )
            if result.rowcount != 1:
                raise ValueError("Payroll payment not found")

    def update_project_finances(
        self,
        project_id: str,
        color: str,
        budget_type: str,
        budget_amount: Decimal,
        budget_minutes: int,
        billable_rate: Decimal,
        client_id: str | None,
    ) -> None:
        if budget_type not in {"none", "hours", "cost"}:
            raise ValueError("Invalid budget type")
        with self.connect() as connection:
            connection.execute(
                """UPDATE projects SET color=%s,budget_type=%s,budget_amount=%s,budget_minutes=%s,billable_rate=%s,client_id=%s WHERE id=%s""",
                (
                    color[:20],
                    budget_type,
                    max(Decimal(0), budget_amount),
                    max(0, budget_minutes),
                    max(Decimal(0), billable_rate),
                    client_id or None,
                    project_id,
                ),
            )

    def web_timer_device(self, user_id: str, project_id: str | None) -> dict[str, Any]:
        """Return a stable virtual device used by the authenticated web timer."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM devices WHERE owner_user_id=%s AND platform='Web timer' LIMIT 1",
                (user_id,),
            ).fetchone()
            if not row:
                device_id = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO devices(id,name,token_hash,enabled,platform,created_at,owner_user_id,project_id)
                       VALUES (%s,'Web timer',%s,TRUE,'Web timer',%s,%s,%s)""",
                    (
                        device_id,
                        token_hash(str(uuid.uuid4())),
                        utc_now(),
                        user_id,
                        project_id,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM devices WHERE id=%s", (device_id,)
                ).fetchone()
        return dict(row)

    def active_timer(self, user_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT ws.*,p.name project_name,t.name task_name,
                          COALESCE(SUM(EXTRACT(EPOCH FROM (COALESCE(seg.ended_at,CURRENT_TIMESTAMP)-seg.started_at))),0)::BIGINT tracked_seconds
                   FROM work_sessions ws JOIN devices d ON d.id=ws.device_id
                   LEFT JOIN projects p ON p.id=ws.project_id LEFT JOIN tasks t ON t.id=ws.task_id
                   LEFT JOIN work_session_segments seg ON seg.session_id=ws.id
                   WHERE ws.user_id=%s AND d.platform='Web timer' AND ws.status IN ('active','paused')
                   GROUP BY ws.id,p.id,t.id ORDER BY ws.started_at DESC LIMIT 1""",
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def create_team(self, name: str, lead_user_id: str | None) -> str:
        team_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO teams(id,name,lead_user_id,created_at) VALUES (%s,%s,%s,%s)",
                (team_id, name.strip()[:120], lead_user_id or None, utc_now()),
            )
        return team_id

    def add_team_member(self, team_id: str, user_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO team_members(team_id,user_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (team_id, user_id),
            )

    def remove_team_member(self, team_id: str, user_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM team_members WHERE team_id=%s AND user_id=%s",
                (team_id, user_id),
            )

    def list_teams(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT t.*,u.email lead_email,u.full_name lead_name,COUNT(tm.user_id) member_count,
                          COALESCE(jsonb_agg(jsonb_build_object('id',mu.id,'email',mu.email,'full_name',mu.full_name))
                          FILTER (WHERE mu.id IS NOT NULL),'[]'::jsonb) members
                   FROM teams t LEFT JOIN users u ON u.id=t.lead_user_id
                   LEFT JOIN team_members tm ON tm.team_id=t.id
                   LEFT JOIN users mu ON mu.id=tm.user_id
                   GROUP BY t.id,u.id ORDER BY lower(t.name)"""
            ).fetchall()
        return [dict(row) for row in rows]

    def create_geofence(
        self,
        name: str,
        project_id: str | None,
        latitude: float,
        longitude: float,
        radius_meters: int,
        enter_action: str = "none",
        exit_action: str = "none",
    ) -> str:
        if enter_action not in {"none", "start"} or exit_action not in {
            "none",
            "stop",
        }:
            raise ValueError("Invalid automatic timer action")
        geofence_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO geofences(id,name,project_id,latitude,longitude,radius_meters,enter_action,exit_action,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    geofence_id,
                    name.strip()[:120],
                    project_id or None,
                    latitude,
                    longitude,
                    radius_meters,
                    enter_action,
                    exit_action,
                    utc_now(),
                ),
            )
        return geofence_id

    def list_geofences(self, user_id: str | None = None) -> list[dict[str, Any]]:
        where = ""
        params: tuple[Any, ...] = ()
        if user_id:
            where = "WHERE g.project_id IS NULL OR EXISTS (SELECT 1 FROM project_members pm WHERE pm.project_id=g.project_id AND pm.user_id=%s)"
            params = (user_id,)
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT g.*,p.name project_name FROM geofences g LEFT JOIN projects p ON p.id=g.project_id
                   """
                + where
                + " ORDER BY g.active DESC,lower(g.name)",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def add_location(self, values: dict[str, Any]) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """INSERT INTO location_events(id,device_id,user_id,session_id,recorded_at,latitude,longitude,accuracy_meters,geofence_id,event_type,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(device_id,recorded_at) DO NOTHING""",
                (
                    values["id"],
                    values["device_id"],
                    values.get("user_id"),
                    values.get("session_id"),
                    values["recorded_at"],
                    values["latitude"],
                    values["longitude"],
                    values["accuracy_meters"],
                    values.get("geofence_id"),
                    values["event_type"],
                    utc_now(),
                ),
            )
        return result.rowcount == 1

    def geofence_state(
        self, user_id: str, latitude: float, longitude: float
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Return the containing fence and the member's previous fence.

        PostgreSQL's earthdistance extension isn't assumed, so use the standard
        haversine formula directly in SQL.
        """
        with self.connect() as connection:
            containing = connection.execute(
                """SELECT g.* FROM geofences g WHERE g.active=TRUE
                   AND (g.project_id IS NULL OR EXISTS (
                     SELECT 1 FROM project_members pm WHERE pm.project_id=g.project_id AND pm.user_id=%s
                   )) AND
                   6371000 * 2 * asin(sqrt(
                     power(sin(radians(%s-g.latitude)/2),2) +
                     cos(radians(g.latitude))*cos(radians(%s))*
                     power(sin(radians(%s-g.longitude)/2),2)
                   )) <= g.radius_meters
                   ORDER BY g.radius_meters LIMIT 1""",
                (user_id, latitude, latitude, longitude),
            ).fetchone()
            previous = connection.execute(
                """SELECT g.* FROM (
                     SELECT geofence_id,event_type FROM location_events
                     WHERE user_id=%s ORDER BY recorded_at DESC LIMIT 1
                   ) le JOIN geofences g ON g.id=le.geofence_id
                   WHERE le.event_type <> 'exit'""",
                (user_id,),
            ).fetchone()
        return (
            dict(containing) if containing else None,
            dict(previous) if previous else None,
        )

    def list_locations(self, user_id: str | None = None) -> list[dict[str, Any]]:
        where, params = ("WHERE le.user_id=%s", (user_id,)) if user_id else ("", ())
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT le.*,u.email,u.full_name,d.name device_name FROM location_events le
                    LEFT JOIN users u ON u.id=le.user_id JOIN devices d ON d.id=le.device_id
                    {where} ORDER BY le.recorded_at DESC LIMIT 500""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def attendance_report(self, user_id: str | None = None) -> list[dict[str, Any]]:
        where, params = ("WHERE s.user_id=%s", (user_id,)) if user_id else ("", ())
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT s.id,u.email,u.full_name,s.starts_at,s.ends_at,p.name project_name,
                    COALESCE(SUM(EXTRACT(EPOCH FROM (LEAST(COALESCE(seg.ended_at,CURRENT_TIMESTAMP),s.ends_at)-GREATEST(seg.started_at,s.starts_at)))) FILTER (WHERE seg.started_at<s.ends_at AND COALESCE(seg.ended_at,CURRENT_TIMESTAMP)>s.starts_at),0)::BIGINT worked_seconds
                    FROM shifts s JOIN users u ON u.id=s.user_id LEFT JOIN projects p ON p.id=s.project_id
                    LEFT JOIN work_sessions ws ON ws.user_id=s.user_id LEFT JOIN work_session_segments seg ON seg.session_id=ws.id
                    {where} GROUP BY s.id,u.id,p.id ORDER BY s.starts_at DESC""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def create_integration(
        self, provider: str, display_name: str, webhook_url: str, creator_id: str
    ) -> str:
        integration_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO integrations(id,provider,display_name,webhook_url,created_by_user_id,created_at,updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (
                    integration_id,
                    provider[:60],
                    display_name.strip()[:120],
                    webhook_url.strip()[:1000],
                    creator_id,
                    utc_now(),
                    utc_now(),
                ),
            )
        return integration_id

    def list_integrations(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM integrations ORDER BY enabled DESC,lower(display_name)"
            ).fetchall()
        return [dict(row) for row in rows]

    def set_integration_enabled(self, integration_id: str, enabled: bool) -> None:
        with self.connect() as connection:
            result = connection.execute(
                "UPDATE integrations SET enabled=%s,updated_at=%s WHERE id=%s",
                (enabled, utc_now(), integration_id),
            )
            if result.rowcount != 1:
                raise ValueError("Integration not found")

    def time_report(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT u.email,p.name project,t.name task,ws.started_at,
                          MAX(seg.ended_at) ended_at,ws.status,COALESCE(SUM(EXTRACT(EPOCH FROM (COALESCE(seg.ended_at,CURRENT_TIMESTAMP)-seg.started_at))),0)::BIGINT seconds,
                          'tracked' time_type
                   FROM work_sessions ws LEFT JOIN users u ON u.id=ws.user_id
                   LEFT JOIN projects p ON p.id=ws.project_id LEFT JOIN tasks t ON t.id=ws.task_id
                   LEFT JOIN work_session_segments seg ON seg.session_id=ws.id
                   GROUP BY ws.id,u.id,p.id,t.id
                   UNION ALL
                   SELECT u.email,p.name,t.name,m.started_at,m.ended_at,m.status,
                          EXTRACT(EPOCH FROM (m.ended_at-m.started_at))::BIGINT,'manual'
                   FROM manual_time_entries m JOIN users u ON u.id=m.user_id
                   JOIN projects p ON p.id=m.project_id LEFT JOIN tasks t ON t.id=m.task_id
                   ORDER BY started_at DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def list_notifications(self, user_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM notifications WHERE user_id=%s OR user_id IS NULL
                   ORDER BY created_at DESC LIMIT 100""",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_notifications_read(self, user_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE notifications SET read_at=%s WHERE user_id=%s AND read_at IS NULL",
                (utc_now(), user_id),
            )

    def create_global_todo(
        self,
        name: str,
        description: str,
        project_ids: list[str],
        add_to_future_projects: bool,
        creator_id: str,
    ) -> str:
        todo_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO global_todos(id,name,description,add_to_future_projects,created_by_user_id,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (
                    todo_id,
                    name.strip()[:160],
                    description.strip()[:500],
                    add_to_future_projects,
                    creator_id,
                    utc_now(),
                ),
            )
            for project_id in project_ids:
                connection.execute(
                    "INSERT INTO project_todos(todo_id,project_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (todo_id, project_id),
                )
        return todo_id

    def list_project_todos(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT gt.*,pt.completed_at,pt.assigned_user_id,u.email assigned_email
                   FROM project_todos pt JOIN global_todos gt ON gt.id=pt.todo_id
                   LEFT JOIN users u ON u.id=pt.assigned_user_id WHERE pt.project_id=%s
                   ORDER BY pt.completed_at NULLS FIRST,gt.created_at DESC""",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_todo_complete(
        self, todo_id: str, project_id: str, user_id: str, complete: bool
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE project_todos SET completed_at=CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END,
                   completed_by_user_id=CASE WHEN %s THEN %s ELSE NULL END
                   WHERE todo_id=%s AND project_id=%s""",
                (complete, complete, user_id, todo_id, project_id),
            )

    def create_scheduled_report(
        self,
        name: str,
        report_type: str,
        frequency: str,
        recipients: str,
        creator_id: str,
    ) -> str:
        if frequency not in {"weekly", "monthly"}:
            raise ValueError("Invalid report frequency")
        report_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO scheduled_reports(id,name,report_type,frequency,recipients,next_send_at,created_by_user_id,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    report_id,
                    name.strip()[:120],
                    report_type[:60],
                    frequency,
                    recipients.strip()[:1000],
                    datetime.now(UTC),
                    creator_id,
                    utc_now(),
                ),
            )
        return report_id

    def list_scheduled_reports(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM scheduled_reports ORDER BY enabled DESC,created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def set_scheduled_report_enabled(self, report_id: str, enabled: bool) -> None:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE scheduled_reports SET enabled=%s,
                   next_send_at=CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE next_send_at END
                   WHERE id=%s""",
                (enabled, enabled, report_id),
            )
            if result.rowcount != 1:
                raise ValueError("Scheduled report not found")

    def delete_scheduled_report(self, report_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM scheduled_reports WHERE id=%s", (report_id,)
            )

    def due_scheduled_reports(self, now: datetime) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM scheduled_reports WHERE enabled=TRUE
                   AND next_send_at IS NOT NULL AND next_send_at <= %s
                   ORDER BY next_send_at FOR UPDATE SKIP LOCKED""",
                (now,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_scheduled_report_sent(
        self, report_id: str, frequency: str, sent_at: datetime
    ) -> None:
        next_at = sent_at + timedelta(days=7 if frequency == "weekly" else 30)
        with self.connect() as connection:
            connection.execute(
                """UPDATE scheduled_reports SET last_sent_at=%s,next_send_at=%s
                   WHERE id=%s""",
                (sent_at, next_at, report_id),
            )

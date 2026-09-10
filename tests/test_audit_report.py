from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from api.config import Settings
from api.main import create_app
from api.security import hash_password
from api.services.retention import RetentionService


def _settings(tmp_path, postgres_url: str) -> Settings:
    return Settings(
        data_dir=tmp_path,
        database_url=postgres_url,
        admin_email="owner@example.test",
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024 * 1024,
        retention_days=30,
        audit_retention_days=365,
    )


def _csrf(response) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def _login(client: TestClient, email: str, password: str) -> None:
    client.cookies.clear()
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf": _csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _member(database, owner, email: str, role: str = "member"):
    _, token = database.create_invitation(email, owner["id"], 24)
    member = database.accept_invitation(
        token, hash_password("member password long enough")
    )
    database.set_user_profile(member["id"], role, email.split("@", 1)[0], 0, 0, 0)
    return database.get_user_any(member["id"])


def test_audit_report_filters_paginates_exports_and_enforces_admin_scope(
    tmp_path, postgres_url
):
    settings = _settings(tmp_path, postgres_url)
    app = create_app(settings)
    with TestClient(app) as client:
        database = app.state.database
        owner = database.get_user_by_email(settings.admin_email)
        manager = _member(database, owner, "manager@example.test", "manager")
        member = _member(database, owner, "member@example.test")
        now = datetime.now(UTC)
        rows = [
            (
                str(uuid.uuid4()),
                owner["id"],
                "project.settings_updated" if index % 2 else "user.updated",
                "user" if index % 2 == 0 else "project",
                member["id"] if index % 2 == 0 else None,
                now - timedelta(minutes=index),
                "=FORMULA()" if index == 0 else f"change={index}",
            )
            for index in range(55)
        ]
        rows.append(
            (
                str(uuid.uuid4()),
                None,
                "retention.completed",
                "audit",
                None,
                now - timedelta(minutes=2),
                "rows=3",
            )
        )
        with database.connect() as connection, connection.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO audit_events(
                       id,actor_user_id,action,target_type,target_id,occurred_at,details
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                rows,
            )
        date_from = (now - timedelta(days=1)).date().isoformat()
        date_to = now.date().isoformat()

        _login(client, owner["email"], settings.admin_password)
        report = client.get(
            "/reports/audit",
            params={"date_from": date_from, "date_to": date_to},
        )
        assert report.status_code == 200
        assert "Audit log" in report.text
        assert "Next" in report.text
        assert "=FORMULA()" in report.text
        second_page = client.get(
            "/reports/audit",
            params={"date_from": date_from, "date_to": date_to, "page": 2},
        )
        assert second_page.status_code == 200
        assert "Previous" in second_page.text

        filtered = client.get(
            "/reports/audit",
            params={
                "date_from": date_from,
                "date_to": date_to,
                "actor_id": "system",
                "action": "retention.completed",
                "target_type": "audit",
            },
        )
        assert filtered.status_code == 200
        assert "rows=3" in filtered.text
        assert "change=1" not in filtered.text
        affected = client.get(
            "/reports/audit",
            params={
                "date_from": date_from,
                "date_to": date_to,
                "member_id": member["id"],
            },
        )
        assert affected.status_code == 200
        assert member["full_name"] in affected.text
        affected_rows = database.audit_report(
            now - timedelta(days=1),
            now + timedelta(days=1),
            target_user_id=member["id"],
        )
        assert affected_rows
        assert all(row["target_type"] == "user" for row in affected_rows)

        exported = client.get(
            "/reports/audit.csv",
            params={"date_from": date_from, "date_to": date_to},
        )
        assert exported.status_code == 200
        assert exported.headers["cache-control"] == "private, no-store"
        assert b"'=FORMULA()" in exported.content
        assert b"actor_user_id" not in exported.content
        pdf = client.get(
            "/reports/audit.pdf",
            params={"date_from": date_from, "date_to": date_to},
        )
        assert pdf.status_code == 200
        assert pdf.content.startswith(b"%PDF")
        assert database.audit_report(
            now - timedelta(days=1),
            now + timedelta(days=1),
            action="report.exported",
        )

        assert (
            client.get(
                "/reports/audit",
                params={"date_from": date_from, "date_to": date_to, "actor_id": "bad"},
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/reports/audit",
                params={
                    "date_from": date_from,
                    "date_to": date_to,
                    "member_id": str(uuid.uuid4()),
                },
            ).status_code
            == 404
        )

        _login(client, manager["email"], "member password long enough")
        assert (
            client.get(
                "/reports/audit", params={"date_from": date_from, "date_to": date_to}
            ).status_code
            == 200
        )
        _login(client, member["email"], "member password long enough")
        assert (
            client.get(
                "/reports/audit", params={"date_from": date_from, "date_to": date_to}
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/reports/audit.csv",
                params={"date_from": date_from, "date_to": date_to},
            ).status_code
            == 403
        )


class _EmptyStorage:
    def delete(self, _key, _version_id=None):
        raise AssertionError("No screenshot should be deleted")


def test_audit_retention_is_bounded_and_records_the_purge(database):
    owner = database.bootstrap_admin("retention-owner@example.test", "hash")
    database.add_audit_event(owner["id"], "old.event", "user", owner["id"])
    with database.connect() as connection:
        connection.execute(
            "UPDATE audit_events SET occurred_at=%s WHERE action='old.event'",
            (datetime.now(UTC) - timedelta(days=31),),
        )
    RetentionService(database, _EmptyStorage(), 30, 30).purge_expired()
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) count FROM audit_events WHERE action='old.event'"
            ).fetchone()["count"]
            == 0
        )
        retained = connection.execute(
            """SELECT details FROM audit_events
               WHERE action='retention.audit_purged'"""
        ).fetchone()
    assert "rows=1" in retained["details"]

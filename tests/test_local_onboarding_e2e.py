"""Opt-in local-stack onboarding test.

The synthetic screenshot and activity source exists only in this pytest module.
It is injected directly into a source checkout's TrackerAgent instance and has no
production setting, command-line option, or packaged-agent code path.
"""

from __future__ import annotations

import html
import os
import re
import time
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from agent import __version__
from agent.activity import ActivitySnapshot
from agent.config import AgentConfig
from agent.main import TrackerAgent
from agent.queue import QueuedRecord, StateEvent, UsageEvent
from api.config import Settings
from api.main import create_app

pytestmark = [
    pytest.mark.local_e2e,
    pytest.mark.skipif(
        os.getenv("DAYFINCH_LOCAL_E2E") != "1",
        reason="set DAYFINCH_LOCAL_E2E=1 to use local Mailpit and MinIO",
    ),
]

MAILPIT_URL = "http://127.0.0.1:8025"
MINIO_URL = "http://127.0.0.1:9000"
MINIO_BUCKET = "dayfinch-local-screenshots"
ADMIN_PASSWORD = "local e2e admin password"
MEMBER_PASSWORD = "local e2e member password"


def _csrf(response: httpx.Response) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert match, response.text
    return match.group(1)


def _login(client: TestClient, email: str, password: str) -> None:
    client.cookies.clear()
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf": _csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def _invite_from_mailpit(recipient: str) -> str:
    deadline = time.monotonic() + 10
    with httpx.Client(base_url=MAILPIT_URL, timeout=2, trust_env=False) as mailpit:
        while time.monotonic() < deadline:
            listing = mailpit.get("/api/v1/messages")
            listing.raise_for_status()
            for message in listing.json().get("messages", []):
                addresses = {
                    str(entry.get("Address", "")).lower()
                    for entry in message.get("To", [])
                }
                if recipient.lower() not in addresses:
                    continue
                detail = mailpit.get(f"/api/v1/message/{message['ID']}")
                detail.raise_for_status()
                body = str(detail.json().get("Text", ""))
                match = re.search(r"https?://[^\s]+/invite/[A-Za-z0-9_-]+", body)
                assert match, body
                return match.group(0)
            time.sleep(0.2)
    pytest.fail(f"Mailpit did not capture the invitation for {recipient}")


def _create_and_accept_invitation(
    client: TestClient, admin_email: str, recipient: str
) -> dict:
    _login(client, admin_email, ADMIN_PASSWORD)
    dashboard = client.get("/")
    invited = client.post(
        "/invitations",
        data={"email": recipient, "csrf": _csrf(dashboard)},
    )
    assert invited.status_code == 200, invited.text
    assert "delivered the private link" in invited.text

    invitation_path = urlparse(_invite_from_mailpit(recipient)).path
    client.cookies.clear()
    invitation_page = client.get(invitation_path)
    accepted = client.post(
        invitation_path,
        data={
            "password": MEMBER_PASSWORD,
            "password_confirm": MEMBER_PASSWORD,
            "csrf": _csrf(invitation_page),
        },
        follow_redirects=False,
    )
    assert accepted.status_code == 303, accepted.text
    user = client.app.state.database.get_user_by_email(recipient)
    assert user and user["enabled"]
    return user


def _enroll_from_project(
    client: TestClient, email: str, project_id: str, device_name: str, directory: Path
) -> tuple[AgentConfig, str]:
    _login(client, email, MEMBER_PASSWORD)
    project_page = client.get(f"/projects/{project_id}")
    enrolled = client.post(
        "/devices",
        data={
            "name": device_name,
            "project_id": project_id,
            "tracker_kind": "desktop",
            "csrf": _csrf(project_page),
        },
    )
    assert enrolled.status_code == 200, enrolled.text
    config_match = re.search(
        r'<textarea id="agentConfig"[^>]*>(.*?)</textarea>',
        enrolled.text,
        re.DOTALL,
    )
    device_match = re.search(r'href="/devices/([^"]+)"', enrolled.text)
    assert config_match and device_match
    config_path = directory / f"{device_name.replace(' ', '-')}.toml"
    config_path.write_text(html.unescape(config_match.group(1)), encoding="utf-8")
    config = replace(
        AgentConfig.from_file(config_path),
        queue_dir=directory / f"queue-{device_match.group(1)}",
        collect_websites=False,
        website_bridge_token="",
    )
    return config, device_match.group(1)


class _TestActivity:
    """Aggregate-only, deterministic activity used by this test and nowhere else."""

    input_available = True

    def start(self) -> bool:
        return True

    def stop(self) -> None:
        return None

    def set_enabled(self, _enabled: bool) -> None:
        return None

    def seconds_since_input(self, _now: float | None = None) -> float:
        return 0.0

    def observe(self, _application: str, *, now: float | None = None) -> None:
        del now

    def snapshot_and_reset(self) -> ActivitySnapshot:
        return ActivitySnapshot(
            keyboard_events=7,
            mouse_clicks=3,
            mouse_distance=140,
            focused_seconds=60,
            interactive_seconds=42,
        )


class _ASGIDeviceClient:
    """TrackerClient contract backed by the app under test."""

    def __init__(self, client: TestClient, token: str):
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}"}

    def configuration(self) -> dict:
        response = self.client.get("/api/v1/configuration", headers=self.headers)
        response.raise_for_status()
        return response.json()

    def heartbeat(self, event: StateEvent) -> str:
        response = self.client.post(
            "/api/v1/heartbeat",
            headers=self.headers,
            json={
                "platform": "local-e2e",
                "event_id": event.id,
                "observed_at": event.observed_at,
                "status": event.status,
                "task_id": event.task_id or None,
                "project_id": event.project_id or None,
                "note": event.note,
                "idle_seconds": event.idle_seconds,
                "heartbeat_interval_seconds": event.heartbeat_interval_seconds,
                "transition": event.transition,
            },
        )
        response.raise_for_status()
        return str(response.json().get("session_id") or "")

    def upload_usage(self, event: UsageEvent) -> None:
        response = self.client.post(
            "/api/v1/usage",
            headers=self.headers,
            json={
                "event_id": event.id,
                "observed_at": event.observed_at,
                "active_app": event.active_app,
                "active_url": event.active_url,
                "focused_seconds": event.focused_seconds,
            },
        )
        response.raise_for_status()

    def upload(self, record: QueuedRecord, screenshot: bytes) -> None:
        response = self.client.post(
            "/api/v1/activity",
            headers=self.headers,
            data=record.fields(__version__),
            files={"screenshot_file": (f"{record.id}.jpg", screenshot, "image/jpeg")},
        )
        response.raise_for_status()

    def close(self) -> None:
        return None


def _test_jpeg() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (80, 45), "#6d5dfc").save(buffer, format="JPEG")
    return buffer.getvalue()


def _run_source_agent(
    client: TestClient,
    config: AgentConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    # Direct monkeypatching is intentionally the only synthetic-frame mechanism.
    monkeypatch.setattr("agent.main.capture_screenshot", lambda **_kwargs: _test_jpeg())
    monkeypatch.setattr("agent.main.is_wayland", lambda: False)
    agent = TrackerAgent(config)
    agent.client.close()
    agent.client = _ASGIDeviceClient(client, config.device_token)
    agent.activity = _TestActivity()
    agent.catalog()
    agent.start_tracking()
    agent._send_heartbeat()
    agent.queue.add_usage("Local E2E Editor", "", 60)
    assert agent._upload_usage_one()
    agent._active_app = "Local E2E Editor"
    agent._capture_to_queue()
    pending = agent.queue.pending(limit=1)
    assert len(pending) == 1
    record_id = pending[0].id
    assert agent._upload_one()
    agent.stop_tracking()
    agent._send_heartbeat(status="stopped")
    agent.client.close()
    return record_id


def _assert_minio_object(app, record_id: str) -> None:
    record = app.state.database.get_record(record_id)
    assert record and record["storage_version_id"]
    response = app.state.storage.client.head_object(
        Bucket=MINIO_BUCKET,
        Key=record["screenshot_path"],
        VersionId=record["storage_version_id"],
    )
    assert response["ContentLength"] > 0


def test_invite_to_first_screenshot_local_stack(
    tmp_path: Path, postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # These credentials are deliberately insecure local-stand-in defaults.
    monkeypatch.setenv(
        "AWS_ACCESS_KEY_ID", os.getenv("DAYFINCH_LOCAL_MINIO_USER", "dayfinch-local")
    )
    monkeypatch.setenv(
        "AWS_SECRET_ACCESS_KEY",
        os.getenv("DAYFINCH_LOCAL_MINIO_PASSWORD", "dayfinch-local-password"),
    )
    monkeypatch.setenv("AWS_SESSION_TOKEN", "")
    # api.main's module-level development app may have initialized boto3 before
    # pytest replaced the environment. Rebuild only boto3's test-process session.
    import boto3

    boto3.setup_default_session()
    suffix = uuid4().hex[:10]
    settings = Settings(
        data_dir=tmp_path,
        admin_password=ADMIN_PASSWORD,
        session_secret="e" * 48,
        cookie_secure=False,
        max_upload_bytes=1024 * 1024,
        retention_days=30,
        admin_email="admin@local-e2e.test",
        database_url=postgres_url,
        public_url="http://127.0.0.1:8000",
        storage_backend="s3",
        s3_bucket=MINIO_BUCKET,
        s3_region="us-east-1",
        s3_endpoint_url=MINIO_URL,
        s3_sse="",
        smtp_host="127.0.0.1",
        smtp_port=1025,
        smtp_from_email="dayfinch-local@example.test",
        smtp_starttls=False,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        first_email = f"first-{suffix}@example.test"
        second_email = f"second-{suffix}@example.test"
        first_user = _create_and_accept_invitation(
            client, settings.admin_email, first_email
        )
        second_user = _create_and_accept_invitation(
            client, settings.admin_email, second_email
        )

        _login(client, settings.admin_email, ADMIN_PASSWORD)
        dashboard = client.get("/")
        created = client.post(
            "/projects",
            data={
                "name": f"Local E2E {suffix}",
                "description": "Opt-in local stand-in test",
                "csrf": _csrf(dashboard),
            },
            follow_redirects=False,
        )
        assert created.status_code == 303, created.text
        project_path = created.headers["location"]
        project_id = project_path.rsplit("/", 1)[-1]
        for user in (first_user, second_user):
            project_page = client.get(project_path)
            assigned = client.post(
                f"/projects/{project_id}/members",
                data={
                    "user_id": user["id"],
                    "project_role": "worker",
                    "csrf": _csrf(project_page),
                },
                follow_redirects=False,
            )
            assert assigned.status_code == 303, assigned.text

        first_config, first_device_id = _enroll_from_project(
            client, first_email, project_id, "First local laptop", tmp_path
        )
        first_record_id = _run_source_agent(client, first_config, monkeypatch)
        _assert_minio_object(app, first_record_id)

        _login(client, settings.admin_email, ADMIN_PASSWORD)
        activity_page = client.get("/activity")
        assert f'/screenshots/{first_record_id}' in activity_page.text
        screenshot = client.get(f"/screenshots/{first_record_id}")
        assert screenshot.status_code == 200
        assert screenshot.headers["content-type"] == "image/jpeg"

        device_page = client.get(f"/devices/{first_device_id}")
        revoked = client.post(
            f"/devices/{first_device_id}/enabled",
            data={"enabled": 0, "csrf": _csrf(device_page)},
            follow_redirects=False,
        )
        assert revoked.status_code == 303
        rejected = QueuedRecord(
            id=str(uuid4()),
            captured_at=datetime.now(UTC).isoformat(),
            keyboard_events=1,
            mouse_clicks=1,
            mouse_distance=1,
            focused_seconds=1,
            interactive_seconds=1,
            session_id="",
            active_app="Rejected local upload",
            screenshot_path="test-only",
        )
        rejection = client.post(
            "/api/v1/activity",
            headers={"Authorization": f"Bearer {first_config.device_token}"},
            data=rejected.fields(__version__),
            files={
                "screenshot_file": ("rejected.jpg", _test_jpeg(), "image/jpeg")
            },
        )
        assert rejection.status_code == 401

        replacement_config, replacement_device_id = _enroll_from_project(
            client, first_email, project_id, "Replacement local laptop", tmp_path
        )
        assert replacement_device_id != first_device_id
        replacement_record_id = _run_source_agent(
            client, replacement_config, monkeypatch
        )
        _assert_minio_object(app, replacement_record_id)

        second_config, _second_device_id = _enroll_from_project(
            client, second_email, project_id, "Second local laptop", tmp_path
        )
        second_record_id = _run_source_agent(client, second_config, monkeypatch)
        _assert_minio_object(app, second_record_id)

        _login(client, first_email, MEMBER_PASSWORD)
        forbidden = client.get(f"/screenshots/{second_record_id}")
        assert forbidden.status_code == 404

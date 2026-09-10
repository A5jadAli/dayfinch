from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from api.config import Settings
from api.main import create_app


def _settings(tmp_path, postgres_url: str) -> Settings:
    return Settings(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="r" * 40,
        cookie_secure=False,
        max_upload_bytes=1024 * 1024,
        retention_days=30,
        admin_email="admin@rate-limit.test",
        database_url=postgres_url,
    )


def _csrf(response) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def _login(client: TestClient, settings: Settings) -> None:
    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "email": settings.admin_email,
            "password": settings.admin_password,
            "csrf": _csrf(page),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def _device(app) -> tuple[str, str]:
    database = app.state.database
    owner = database.get_user_by_email(app.state.settings.admin_email)
    project = database.create_project(f"Rate limit {uuid4()}", "", owner["id"])
    database.add_project_member(project["id"], owner["id"])
    _device_record, token = database.create_device(
        "Replay laptop", owner["id"], project["id"]
    )
    return token, project["id"]


def test_anonymous_limit_retry_after_and_health_exemption(tmp_path, postgres_url):
    settings = replace(
        _settings(tmp_path, postgres_url),
        anonymous_request_limit=2,
        # Keep this test from straddling a real fixed-window boundary.
        rate_limit_window_seconds=3600,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/login").status_code == 200
        assert client.get("/login").status_code == 200
        for _ in range(4):
            assert client.get("/health").status_code == 200
            assert client.get("/livez").status_code == 200
            assert client.get("/readyz").status_code == 200
        blocked = client.get("/login")
    assert blocked.status_code == 429
    assert 1 <= int(blocked.headers["retry-after"]) <= 3600
    assert blocked.json() == {"detail": "Too many requests"}


def test_signed_in_web_requests_use_their_own_limit(tmp_path, postgres_url):
    settings = replace(
        _settings(tmp_path, postgres_url),
        anonymous_request_limit=20,
        web_request_limit=2,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        _login(client, settings)
        assert client.get("/").status_code == 200
        assert client.get("/").status_code == 200
        blocked = client.get("/")
    assert blocked.status_code == 429
    assert "retry-after" in blocked.headers


def test_rate_limit_count_is_shared_across_app_replicas(tmp_path, postgres_url):
    settings = replace(
        _settings(tmp_path, postgres_url), anonymous_request_limit=2
    )
    first_app = create_app(settings)
    second_app = create_app(settings)
    with TestClient(first_app) as first, TestClient(second_app) as second:
        assert first.get("/login").status_code == 200
        assert second.get("/login").status_code == 200
        assert first.get("/login").status_code == 429


def test_device_limit_keeps_a_separate_offline_replay_burst(
    tmp_path, postgres_url
):
    settings = replace(
        _settings(tmp_path, postgres_url),
        device_request_limit=1,
        device_replay_request_limit=3,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        token, project_id = _device(app)
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/v1/configuration", headers=headers).status_code == 200
        normal_blocked = client.get("/api/v1/configuration", headers=headers)
        assert normal_blocked.status_code == 429

        for _ in range(3):
            replay = client.post(
                "/api/v1/heartbeat",
                headers=headers,
                json={
                    "platform": "rate-limit-test",
                    "event_id": str(uuid4()),
                    "observed_at": datetime.now(UTC).isoformat(),
                    "status": "active",
                    "project_id": project_id,
                    "heartbeat_interval_seconds": 60,
                },
            )
            assert replay.status_code == 200, replay.text
        replay_blocked = client.post(
            "/api/v1/heartbeat",
            headers=headers,
            json={"platform": "rate-limit-test", "status": "active"},
        )
    assert replay_blocked.status_code == 429
    assert "retry-after" in replay_blocked.headers


def test_request_limit_environment_overrides(monkeypatch):
    monkeypatch.setenv("TRACKER_RATE_LIMIT_WINDOW_SECONDS", "7")
    monkeypatch.setenv("TRACKER_ANONYMOUS_REQUEST_LIMIT", "11")
    monkeypatch.setenv("TRACKER_WEB_REQUEST_LIMIT", "22")
    monkeypatch.setenv("TRACKER_DEVICE_REQUEST_LIMIT", "33")
    monkeypatch.setenv("TRACKER_DEVICE_REPLAY_REQUEST_LIMIT", "44")

    settings = Settings.from_env()

    assert settings.rate_limit_window_seconds == 7
    assert settings.anonymous_request_limit == 11
    assert settings.web_request_limit == 22
    assert settings.device_request_limit == 33
    assert settings.device_replay_request_limit == 44

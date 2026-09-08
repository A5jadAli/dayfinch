from __future__ import annotations

import platform
from io import BytesIO

import httpx

from .queue import QueuedRecord, StateEvent, UsageEvent


class TrackerClient:
    def __init__(self, server_url: str, device_token: str, agent_version: str):
        self.agent_version = agent_version
        # Constant for the life of the process; rebuilding it per heartbeat is waste.
        self._platform = platform.platform()
        self._client = httpx.Client(
            base_url=server_url,
            headers={"Authorization": f"Bearer {device_token}"},
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=False,
        )

    def heartbeat(self, event: StateEvent) -> str:
        response = self._client.post(
            "/api/v1/heartbeat",
            json={
                "platform": self._platform,
                "event_id": event.id,
                "observed_at": event.observed_at,
                "status": event.status,
                "task_id": event.task_id or None,
                "project_id": event.project_id or None,
                "note": event.note,
                "idle_seconds": event.idle_seconds,
                "heartbeat_interval_seconds": event.heartbeat_interval_seconds,
            },
        )
        response.raise_for_status()
        return str(response.json().get("session_id") or "")

    def configuration(self) -> dict:
        response = self._client.get("/api/v1/configuration")
        response.raise_for_status()
        return response.json()

    def upload_usage(self, event: UsageEvent) -> None:
        response = self._client.post(
            "/api/v1/usage",
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
        with BytesIO(screenshot) as image:
            response = self._client.post(
                "/api/v1/activity",
                data=record.fields(self.agent_version),
                files={"screenshot_file": (f"{record.id}.jpg", image, "image/jpeg")},
            )
        response.raise_for_status()

    def close(self) -> None:
        self._client.close()

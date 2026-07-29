from __future__ import annotations

import platform
from pathlib import Path

import httpx

from .queue import QueuedRecord, StateEvent


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
                "idle_seconds": event.idle_seconds,
                "heartbeat_interval_seconds": event.heartbeat_interval_seconds,
            },
        )
        response.raise_for_status()
        return str(response.json().get("session_id") or "")

    def upload(self, record: QueuedRecord) -> None:
        path = Path(record.screenshot_path)
        with path.open("rb") as image:
            response = self._client.post(
                "/api/v1/activity",
                data=record.fields(self.agent_version),
                files={"screenshot_file": (path.name, image, "image/jpeg")},
            )
        response.raise_for_status()

    def close(self) -> None:
        self._client.close()

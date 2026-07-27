from __future__ import annotations

import platform
from pathlib import Path

import httpx

from .queue import QueuedRecord


class TrackerClient:
    def __init__(self, server_url: str, device_token: str, agent_version: str):
        self.agent_version = agent_version
        self._client = httpx.Client(
            base_url=server_url,
            headers={"Authorization": f"Bearer {device_token}"},
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=False,
        )

    def heartbeat(self, status: str, task_id: str = "") -> str:
        response = self._client.post(
            "/api/v1/heartbeat",
            json={
                "platform": platform.platform(),
                "status": status,
                "task_id": task_id or None,
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

from __future__ import annotations

import ipaddress
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class AgentConfig:
    server_url: str
    device_token: str
    consent_confirmed: bool
    task_id: str = ""
    capture_interval_seconds: int = 600
    heartbeat_interval_seconds: int = 60
    jpeg_quality: int = 65
    capture_all_monitors: bool = True
    max_image_dimension: int = 1920
    idle_timeout_seconds: int = 1800
    collect_websites: bool = True
    website_bridge_token: str = ""
    website_bridge_port: int = 8765
    max_queue_items: int = 500
    queue_dir: Path = Path("runtime/agent-queue")

    @classmethod
    def from_file(cls, path: Path) -> AgentConfig:
        with path.open("rb") as stream:
            values = tomllib.load(stream)
        config = cls(
            server_url=str(values.get("server_url", "")).rstrip("/"),
            device_token=str(values.get("device_token", "")),
            consent_confirmed=bool(values.get("consent_confirmed", False)),
            task_id=str(values.get("task_id", "")).strip(),
            capture_interval_seconds=int(values.get("capture_interval_seconds", 600)),
            heartbeat_interval_seconds=int(
                values.get("heartbeat_interval_seconds", 60)
            ),
            jpeg_quality=int(values.get("jpeg_quality", 65)),
            capture_all_monitors=bool(values.get("capture_all_monitors", True)),
            max_image_dimension=int(values.get("max_image_dimension", 1920)),
            idle_timeout_seconds=int(values.get("idle_timeout_seconds", 1800)),
            collect_websites=bool(values.get("collect_websites", True)),
            website_bridge_token=str(values.get("website_bridge_token", "")).strip(),
            website_bridge_port=int(values.get("website_bridge_port", 8765)),
            max_queue_items=int(values.get("max_queue_items", 500)),
            queue_dir=(
                path.parent / values.get("queue_dir", "runtime/agent-queue")
            ).resolve(),
        )
        config.validate()
        return config

    def validate(self) -> None:
        parsed = urlparse(self.server_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("server_url must be an HTTP(S) URL")
        if parsed.scheme != "https" and not self._is_loopback(parsed.hostname):
            raise ValueError("HTTPS is required unless the server is on localhost")
        if len(self.device_token) < 32 or "paste-the" in self.device_token:
            raise ValueError("device_token must be a real enrollment token")
        if not self.consent_confirmed:
            raise ValueError(
                "Tracking is disabled until the device owner sets consent_confirmed = true"
            )
        if self.task_id:
            try:
                import uuid

                uuid.UUID(self.task_id)
            except ValueError as exc:
                raise ValueError(
                    "task_id must be a UUID from the project page"
                ) from exc
        if not 60 <= self.capture_interval_seconds <= 86_400:
            raise ValueError("capture_interval_seconds must be between 60 and 86400")
        if not 15 <= self.heartbeat_interval_seconds <= 3_600:
            raise ValueError("heartbeat_interval_seconds must be between 15 and 3600")
        if not 30 <= self.jpeg_quality <= 90:
            raise ValueError("jpeg_quality must be between 30 and 90")
        if not 10 <= self.max_queue_items <= 10_000:
            raise ValueError("max_queue_items must be between 10 and 10000")
        if (
            self.max_image_dimension != 0
            and not 640 <= self.max_image_dimension <= 7_680
        ):
            raise ValueError(
                "max_image_dimension must be 0 (full size) or between 640 and 7680"
            )
        if (
            self.idle_timeout_seconds != 0
            and not 60 <= self.idle_timeout_seconds <= 86_400
        ):
            raise ValueError(
                "idle_timeout_seconds must be 0 (disabled) or between 60 and 86400"
            )
        if self.website_bridge_token and len(self.website_bridge_token) < 32:
            raise ValueError("website_bridge_token must contain at least 32 characters")
        if not 1024 <= self.website_bridge_port <= 65_535:
            raise ValueError("website_bridge_port must be between 1024 and 65535")

    @staticmethod
    def _is_loopback(hostname: str) -> bool:
        if hostname.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return False

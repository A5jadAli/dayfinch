from __future__ import annotations

import base64
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
    project_id: str = ""
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
    update_manifest_url: str = ""
    update_public_key: str = ""
    update_mode: str = "notify"

    @classmethod
    def from_file(cls, path: Path) -> AgentConfig:
        with path.open("rb") as stream:
            values = tomllib.load(stream)
        config = cls(
            server_url=str(values.get("server_url", "")).rstrip("/"),
            device_token=str(values.get("device_token", "")),
            consent_confirmed=bool(values.get("consent_confirmed", False)),
            project_id=str(values.get("project_id", "")).strip(),
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
            update_manifest_url=str(values.get("update_manifest_url", "")).strip(),
            update_public_key=str(values.get("update_public_key", "")).strip(),
            update_mode=str(values.get("update_mode", "notify")).strip().lower(),
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
        for field, value in (
            ("project_id", self.project_id),
            ("task_id", self.task_id),
        ):
            if not value:
                continue
            try:
                import uuid

                uuid.UUID(value)
            except ValueError as exc:
                raise ValueError(
                    f"{field} must be a UUID from the project page"
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
        if bool(self.update_manifest_url) != bool(self.update_public_key):
            raise ValueError(
                "update_manifest_url and update_public_key must be configured together"
            )
        if self.update_mode not in {"off", "notify", "download"}:
            raise ValueError("update_mode must be off, notify, or download")
        if self.update_manifest_url:
            parsed_update = urlparse(self.update_manifest_url)
            if (
                parsed_update.scheme not in {"http", "https"}
                or not parsed_update.hostname
                or parsed_update.username
                or parsed_update.password
                or parsed_update.fragment
            ):
                raise ValueError("update_manifest_url must be a safe HTTP(S) URL")
            if parsed_update.scheme != "https" and not self._is_loopback(
                parsed_update.hostname
            ):
                raise ValueError(
                    "update_manifest_url requires HTTPS unless hosted on localhost"
                )
            try:
                padding = "=" * (-len(self.update_public_key) % 4)
                public_key = base64.b64decode(
                    self.update_public_key + padding,
                    altchars=b"-_",
                    validate=True,
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("update_public_key must be valid base64url") from exc
            if len(public_key) != 32:
                raise ValueError("update_public_key must decode to 32 bytes")

    @staticmethod
    def _is_loopback(hostname: str) -> bool:
        if hostname.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return False

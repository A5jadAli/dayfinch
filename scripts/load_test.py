from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

JPEG_PAYLOAD = b"\xff\xd8\xff\xd9"


class LoadTestError(RuntimeError):
    pass


@dataclass
class Measurements:
    latencies: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    statuses: Counter[str] = field(default_factory=Counter)

    def record(self, endpoint: str, elapsed: float, status_code: int | None) -> None:
        self.latencies[endpoint].append(elapsed)
        status = str(status_code) if status_code is not None else "transport_error"
        self.statuses[f"{endpoint}:{status}"] += 1

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0
        ordered = sorted(values)
        index = max(0, math.ceil(len(ordered) * percentile) - 1)
        return round(ordered[index] * 1000, 3)

    def report(self, duration: float, workers: int) -> dict[str, object]:
        attempts = sum(self.statuses.values())
        errors = sum(
            count
            for label, count in self.statuses.items()
            if label.endswith("transport_error") or int(label.rsplit(":", 1)[1]) >= 400
        )
        endpoints = {
            endpoint: {
                "count": len(values),
                "p50_ms": self._percentile(values, 0.50),
                "p95_ms": self._percentile(values, 0.95),
                "p99_ms": self._percentile(values, 0.99),
                "max_ms": round(max(values, default=0) * 1000, 3),
            }
            for endpoint, values in sorted(self.latencies.items())
        }
        return {
            "duration_seconds": round(duration, 3),
            "workers": workers,
            "attempts": attempts,
            "requests_per_second": round(attempts / duration, 3) if duration else 0,
            "errors": errors,
            "error_percent": round(errors * 100 / attempts, 3) if attempts else 0,
            "statuses": dict(sorted(self.statuses.items())),
            "endpoints": endpoints,
        }


def read_tokens(path: Path) -> list[str]:
    try:
        values = [line.strip() for line in path.read_text().splitlines()]
    except OSError as exc:
        raise LoadTestError(f"Unable to read token file: {path}") from exc
    tokens = list(
        dict.fromkeys(value for value in values if value and not value.startswith("#"))
    )
    if not tokens or any(
        len(token) < 32 or any(char.isspace() for char in token) for token in tokens
    ):
        raise LoadTestError("Token file must contain one valid device token per line")
    return tokens


def validate_target(base_url: str, acknowledge_remote: bool) -> str:
    parsed = urlparse(base_url.rstrip("/"))
    local_hosts = {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LoadTestError("Base URL must be a valid HTTP(S) origin")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise LoadTestError("Base URL must not contain a path, query, or fragment")
    if parsed.scheme != "https" and parsed.hostname not in local_hosts:
        raise LoadTestError("Remote load-test targets must use HTTPS")
    if parsed.hostname not in local_hosts and not acknowledge_remote:
        raise LoadTestError("Remote execution requires --acknowledge-production-impact")
    return base_url.rstrip("/")


async def _request(
    client: httpx.AsyncClient,
    measurements: Measurements,
    endpoint: str,
    *,
    headers: dict[str, str],
    json_body: dict[str, object] | None = None,
    data: dict[str, object] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
) -> httpx.Response | None:
    started = time.perf_counter()
    try:
        response = await client.post(
            endpoint, headers=headers, json=json_body, data=data, files=files
        )
    except httpx.HTTPError:
        measurements.record(endpoint, time.perf_counter() - started, None)
        return None
    measurements.record(endpoint, time.perf_counter() - started, response.status_code)
    return response


async def worker(
    client: httpx.AsyncClient,
    token: str,
    measurements: Measurements,
    *,
    stop_at: float,
    heartbeat_interval: float,
    capture_interval: float,
    project_id: str,
    task_id: str,
) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    next_heartbeat = time.monotonic()
    next_capture = time.monotonic()
    session_id = ""
    try:
        while time.monotonic() < stop_at:
            now = time.monotonic()
            observed_at = datetime.now(UTC).isoformat()
            if now >= next_heartbeat:
                response = await _request(
                    client,
                    measurements,
                    "/api/v1/heartbeat",
                    headers=headers,
                    json_body={
                        "platform": "Dayfinch capacity harness",
                        "status": "active",
                        "project_id": project_id or None,
                        "task_id": task_id or None,
                        "event_id": str(uuid.uuid4()),
                        "observed_at": observed_at,
                        "heartbeat_interval_seconds": max(1, round(heartbeat_interval)),
                    },
                )
                if response is not None and response.is_success:
                    session_id = str(response.json().get("session_id") or "")
                next_heartbeat = now + heartbeat_interval
            if now >= next_capture:
                await _request(
                    client,
                    measurements,
                    "/api/v1/activity",
                    headers=headers,
                    data={
                        "record_id": str(uuid.uuid4()),
                        "captured_at": observed_at,
                        "keyboard_events": 8,
                        "mouse_clicks": 2,
                        "mouse_distance": 400,
                        "active_app": "Capacity harness",
                        "agent_version": "load-test",
                        "focused_seconds": min(60, round(capture_interval)),
                        "interactive_seconds": min(45, round(capture_interval)),
                        "session_id": session_id,
                    },
                    files={
                        "screenshot_file": ("capture.jpg", JPEG_PAYLOAD, "image/jpeg")
                    },
                )
                next_capture = now + capture_interval
            await asyncio.sleep(min(0.1, max(0, stop_at - time.monotonic())))
    finally:
        await _request(
            client,
            measurements,
            "/api/v1/heartbeat",
            headers=headers,
            json_body={
                "platform": "Dayfinch capacity harness",
                "status": "stopped",
                "project_id": project_id or None,
                "task_id": task_id or None,
                "event_id": str(uuid.uuid4()),
                "observed_at": datetime.now(UTC).isoformat(),
                "heartbeat_interval_seconds": max(1, round(heartbeat_interval)),
            },
        )


async def run(arguments: argparse.Namespace) -> dict[str, object]:
    base_url = validate_target(
        arguments.base_url, arguments.acknowledge_production_impact
    )
    tokens = read_tokens(arguments.tokens_file)
    measurements = Measurements()
    started = time.monotonic()
    stop_at = started + arguments.duration
    limits = httpx.Limits(
        max_connections=len(tokens), max_keepalive_connections=len(tokens)
    )
    timeout = httpx.Timeout(arguments.timeout)
    async with httpx.AsyncClient(
        base_url=base_url, timeout=timeout, limits=limits
    ) as client:
        await asyncio.gather(
            *(
                worker(
                    client,
                    token,
                    measurements,
                    stop_at=stop_at,
                    heartbeat_interval=arguments.heartbeat_interval,
                    capture_interval=arguments.capture_interval,
                    project_id=arguments.project_id,
                    task_id=arguments.task_id,
                )
                for token in tokens
            )
        )
    return measurements.report(time.monotonic() - started, len(tokens))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="dayfinch-load-test",
        description="Drive real Dayfinch heartbeat and screenshot ingestion paths",
    )
    result.add_argument("--base-url", default="http://127.0.0.1:8000")
    result.add_argument("--tokens-file", type=Path, required=True)
    result.add_argument("--duration", type=float, default=60)
    result.add_argument("--heartbeat-interval", type=float, default=5)
    result.add_argument("--capture-interval", type=float, default=15)
    result.add_argument("--timeout", type=float, default=15)
    result.add_argument("--project-id", default="")
    result.add_argument("--task-id", default="")
    result.add_argument("--acknowledge-production-impact", action="store_true")
    return result


def main() -> None:
    arguments = parser().parse_args()
    for label in ("duration", "heartbeat_interval", "capture_interval", "timeout"):
        if getattr(arguments, label) <= 0:
            raise SystemExit(f"--{label.replace('_', '-')} must be positive")
    try:
        report = asyncio.run(run(arguments))
    except (LoadTestError, KeyboardInterrupt) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

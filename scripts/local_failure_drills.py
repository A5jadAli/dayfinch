"""Run disruptive, local-only dependency recovery drills.

This script is intentionally restricted to a loopback Dayfinch instance and the
repository's opt-in Mailpit/MinIO Compose override. It uses a tiny synthetic JPEG;
it never captures a screen and it exposes no production configuration switch.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import tempfile
import time
import tomllib
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from agent import __version__
from agent.activity import ActivitySnapshot
from agent.client import TrackerClient
from agent.queue import OfflineQueue

SYNTHETIC_LOCAL_JPEG = b"\xff\xd8\xff\xd9"
COMPOSE_FILES = ("compose.yaml", "compose.local.yaml")


class DrillError(RuntimeError):
    pass


@dataclass
class DrillResult:
    name: str
    expected: str
    actual: str
    elapsed_seconds: float


def validate_local_target(value: str) -> str:
    parsed = urlparse(value.rstrip("/"))
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise DrillError("Failure drills are restricted to a loopback HTTP(S) origin")
    return value.rstrip("/")


def _csrf(response: httpx.Response) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', response.text)
    if not match:
        raise DrillError(f"No CSRF token found on {response.request.url.path}")
    return match.group(1)


def _require(response: httpx.Response, status_code: int, operation: str) -> None:
    if response.status_code != status_code:
        raise DrillError(
            f"{operation} returned HTTP {response.status_code}: {response.text[:240]}"
        )


class ComposeControl:
    def __init__(self, directory: Path):
        self.directory = directory
        self.command = ["docker", "compose"]
        for name in COMPOSE_FILES:
            self.command.extend(("-f", name))

    def run(self, *arguments: str) -> None:
        try:
            subprocess.run(
                [*self.command, *arguments],
                cwd=self.directory,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=180,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            output = getattr(exc, "stdout", "") or ""
            raise DrillError(
                f"Compose command failed: {' '.join(arguments)}: {output[-500:]}"
            ) from exc

    def stop(self, service: str) -> None:
        self.run("stop", "--timeout", "10", service)

    def start(self, service: str) -> None:
        self.run("up", "-d", service)


def _wait_http(
    url: str,
    *,
    wanted_status: int = 200,
    timeout: float = 60,
) -> httpx.Response:
    deadline = time.monotonic() + timeout
    last = "no response"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=5, trust_env=False)
            last = f"HTTP {response.status_code}"
            if response.status_code == wanted_status:
                return response
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        time.sleep(0.5)
    raise DrillError(f"Timed out waiting for {url} ({last})")


def _login(client: httpx.Client, email: str, password: str) -> None:
    client.cookies.clear()
    page = client.get("/login")
    _require(page, 200, "login page")
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf": _csrf(page)},
        follow_redirects=False,
    )
    _require(response, 303, "login")


def _create_failed_invitation(
    client: httpx.Client, email: str
) -> tuple[str, DrillResult]:
    started = time.monotonic()
    dashboard = client.get("/")
    _require(dashboard, 200, "dashboard")
    response = client.post(
        "/invitations", data={"email": email, "csrf": _csrf(dashboard)}
    )
    _require(response, 200, "invitation creation")
    if "Email delivery failed" not in response.text:
        raise DrillError("SMTP outage was not reported on the invitation result")
    match = re.search(r'value="([^"]+/invite/[A-Za-z0-9_-]+)"', response.text)
    if not match:
        raise DrillError("The failed-delivery page did not retain the invitation link")
    invitation_url = html.unescape(match.group(1))
    client.cookies.clear()
    invitation = client.get(urlparse(invitation_url).path)
    _require(invitation, 200, "retained invitation link")
    return invitation_url, DrillResult(
        name="smtp_unavailable_during_invitation",
        expected="The invitation remains valid and the UI reports SMTP delivery failure.",
        actual="The UI reported delivery failure and the retained one-time link returned 200.",
        elapsed_seconds=round(time.monotonic() - started, 3),
    )


def _accept_invitation(client: httpx.Client, invitation_url: str, password: str) -> None:
    path = urlparse(invitation_url).path
    page = client.get(path)
    _require(page, 200, "invitation acceptance page")
    response = client.post(
        path,
        data={
            "password": password,
            "password_confirm": password,
            "csrf": _csrf(page),
        },
        follow_redirects=False,
    )
    _require(response, 303, "invitation acceptance")


def _create_project_and_device(
    client: httpx.Client,
    *,
    admin_email: str,
    admin_password: str,
    member_email: str,
    member_password: str,
    suffix: str,
) -> tuple[str, str]:
    _login(client, admin_email, admin_password)
    dashboard = client.get("/")
    response = client.post(
        "/projects",
        data={
            "name": f"Local failure drill {suffix}",
            "description": "Disposable local-only recovery drill",
            "csrf": _csrf(dashboard),
        },
        follow_redirects=False,
    )
    _require(response, 303, "project creation")
    project_path = response.headers["location"]
    project_id = project_path.rsplit("/", 1)[-1]
    project_page = client.get(project_path)
    user_match = re.search(
        rf'<option value="([^"]+)">{re.escape(member_email)}</option>',
        project_page.text,
        re.IGNORECASE,
    )
    if not user_match:
        raise DrillError("The invited employee was not available for project assignment")
    assigned = client.post(
        f"/projects/{project_id}/members",
        data={
            "user_id": user_match.group(1),
            "project_role": "worker",
            "csrf": _csrf(project_page),
        },
        follow_redirects=False,
    )
    _require(assigned, 303, "project assignment")

    _login(client, member_email, member_password)
    member_project = client.get(project_path)
    enrolled = client.post(
        "/devices",
        data={
            "name": f"Failure drill device {suffix}",
            "project_id": project_id,
            "tracker_kind": "desktop",
            "csrf": _csrf(member_project),
        },
    )
    _require(enrolled, 200, "device enrollment")
    config_match = re.search(
        r'<textarea id="agentConfig"[^>]*>(.*?)</textarea>', enrolled.text, re.DOTALL
    )
    if not config_match:
        raise DrillError("Device enrollment did not return a source-agent configuration")
    config = tomllib.loads(html.unescape(config_match.group(1)))
    token = str(config.get("device_token", ""))
    if len(token) < 32:
        raise DrillError("Device enrollment returned an invalid token")
    return project_id, token


def _queued_capture(queue: OfflineQueue, label: str):
    return queue.add(
        SYNTHETIC_LOCAL_JPEG,
        ActivitySnapshot(3, 1, 40, focused_seconds=15, interactive_seconds=10),
        f"Local-only {label}",
    )


def _expect_upload_failure(client: TrackerClient, queue: OfflineQueue) -> str:
    record = queue.pending(limit=1)[0]
    try:
        client.upload(record, queue.read_screenshot(record))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code < 500:
            raise DrillError(
                f"Upload was permanently rejected with HTTP {exc.response.status_code}"
            ) from exc
        if queue.count() != 1:
            raise DrillError("The encrypted screenshot queue changed after failure") from exc
        return type(exc).__name__
    except (httpx.TransportError, OSError) as exc:
        if queue.count() != 1:
            raise DrillError("The encrypted screenshot queue changed after failure") from exc
        return type(exc).__name__
    raise DrillError("The upload unexpectedly succeeded during the outage")


def _retry_capture(client: TrackerClient, queue: OfflineQueue) -> str:
    record = queue.pending(limit=1)[0]
    client.upload(record, queue.read_screenshot(record))
    queue.acknowledge(record)
    if queue.count() != 0:
        raise DrillError("The recovered screenshot remained in the offline queue")
    return record.id


def run_drills(arguments: argparse.Namespace) -> list[DrillResult]:
    base_url = validate_local_target(arguments.base_url)
    password = os.getenv(arguments.admin_password_env, "")
    if not password:
        raise DrillError(f"{arguments.admin_password_env} is required")
    repository = Path(__file__).resolve().parents[1]
    compose = ComposeControl(repository)
    suffix = uuid.uuid4().hex[:10]
    member_email = f"failure-drill-{suffix}@example.test"
    member_password = f"local failure drill {suffix} password"
    results: list[DrillResult] = []

    with httpx.Client(
        base_url=base_url, timeout=httpx.Timeout(20, connect=5), trust_env=False
    ) as web:
        _login(web, arguments.admin_email, password)
        compose.stop("mailpit")
        try:
            invitation_url, result = _create_failed_invitation(web, member_email)
            results.append(result)
        finally:
            compose.start("mailpit")
            _wait_http("http://127.0.0.1:8025/api/v1/messages")

        _accept_invitation(web, invitation_url, member_password)
        project_id, device_token = _create_project_and_device(
            web,
            admin_email=arguments.admin_email,
            admin_password=password,
            member_email=member_email,
            member_password=member_password,
            suffix=suffix,
        )

        with tempfile.TemporaryDirectory(prefix="dayfinch-failure-drill-") as temporary:
            queue = OfflineQueue(Path(temporary) / "queue", 20, device_token)
            tracker = TrackerClient(base_url, device_token, __version__)
            try:
                initial = queue.add_state(
                    "active",
                    project_id=project_id,
                    heartbeat_interval_seconds=15,
                    transition=True,
                )
                tracker.heartbeat(initial)
                queue.acknowledge_state(initial)
                _queued_capture(queue, "MinIO outage")
                started = time.monotonic()
                compose.stop("minio")
                try:
                    failure = _expect_upload_failure(tracker, queue)
                    ready = _wait_http(f"{base_url}/readyz")
                    if ready.json() != {"status": "ready"}:
                        raise DrillError("Readiness changed despite PostgreSQL being healthy")
                finally:
                    compose.start("minio")
                    _wait_http("http://127.0.0.1:9000/minio/health/ready")
                minio_record = _retry_capture(tracker, queue)
                results.append(
                    DrillResult(
                        name="minio_unavailable_during_upload",
                        expected="The encrypted agent queue retains the capture and retries after MinIO recovery.",
                        actual=f"Upload failed with {failure}; queue stayed at 1, retry succeeded, queue became 0.",
                        elapsed_seconds=round(time.monotonic() - started, 3),
                    )
                )

                pending = queue.add_state(
                    "active",
                    project_id=project_id,
                    heartbeat_interval_seconds=15,
                    transition=False,
                )
                started = time.monotonic()
                compose.stop("postgres")
                try:
                    heartbeat_failure = ""
                    try:
                        tracker.heartbeat(pending)
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code < 500:
                            raise DrillError(
                                "Heartbeat received a permanent rejection during "
                                "the PostgreSQL outage"
                            ) from exc
                        heartbeat_failure = type(exc).__name__
                    except (httpx.TransportError, OSError) as exc:
                        heartbeat_failure = type(exc).__name__
                    if not heartbeat_failure or queue.state_count() != 1:
                        raise DrillError("Active heartbeat was not retained during PostgreSQL outage")
                    unavailable = _wait_http(
                        f"{base_url}/readyz", wanted_status=503, timeout=50
                    )
                    if unavailable.json() != {"status": "unavailable"}:
                        raise DrillError("Readiness did not report PostgreSQL unavailable")
                finally:
                    compose.start("postgres")
                    _wait_http(f"{base_url}/readyz", timeout=90)
                tracker.heartbeat(pending)
                queue.acknowledge_state(pending)
                if queue.state_count() != 0:
                    raise DrillError("Recovered active heartbeat remained queued")
                results.append(
                    DrillResult(
                        name="postgres_restart_during_tracking",
                        expected="Readiness returns 503 and the active heartbeat replays after PostgreSQL recovers.",
                        actual=f"Heartbeat failed with {heartbeat_failure}; /readyz returned 503, then replay emptied the queue.",
                        elapsed_seconds=round(time.monotonic() - started, 3),
                    )
                )

                _queued_capture(queue, "server restart")
                started = time.monotonic()
                compose.stop("dayfinch-server")
                try:
                    server_failure = _expect_upload_failure(tracker, queue)
                finally:
                    compose.start("dayfinch-server")
                    _wait_http(f"{base_url}/readyz", timeout=90)
                server_record = _retry_capture(tracker, queue)
                results.append(
                    DrillResult(
                        name="server_restart_during_upload",
                        expected="The encrypted agent queue retains the capture and retries after server recovery.",
                        actual=f"Upload failed with {server_failure}; queue stayed at 1, retry succeeded, queue became 0.",
                        elapsed_seconds=round(time.monotonic() - started, 3),
                    )
                )

                stopped = queue.add_state(
                    "stopped", heartbeat_interval_seconds=15, transition=True
                )
                tracker.heartbeat(stopped)
                queue.acknowledge_state(stopped)
            finally:
                tracker.close()

        _login(web, arguments.admin_email, password)
        activity = web.get("/activity")
        _require(activity, 200, "post-recovery activity page")
        missing = [
            record_id
            for record_id in (minio_record, server_record)
            if f"/screenshots/{record_id}" not in activity.text
        ]
        if missing:
            raise DrillError("Recovered records were missing from admin activity")
        for result in results:
            result.actual += " Data remained visible after recovery."
    return results


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--base-url", default="http://127.0.0.1:8000")
    result.add_argument("--admin-email", default="admin@example.local")
    result.add_argument(
        "--admin-password-env", default="TRACKER_ADMIN_PASSWORD"
    )
    result.add_argument(
        "--acknowledge-local-disruption",
        action="store_true",
        help="Confirm that local Compose services may be stopped and restarted",
    )
    return result


def main() -> None:
    arguments = parser().parse_args()
    if not arguments.acknowledge_local_disruption:
        raise SystemExit("Pass --acknowledge-local-disruption to run the local drill")
    try:
        results = run_drills(arguments)
    except (DrillError, KeyboardInterrupt) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps([asdict(result) for result in results], indent=2))


if __name__ == "__main__":
    main()

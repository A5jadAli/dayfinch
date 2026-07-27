from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

from . import __version__
from .active_app import active_application
from .activity import ActivityMonitor
from .capture import capture_screenshot
from .client import TrackerClient
from .config import AgentConfig
from .diagnostics import format_diagnostics, has_failures, run_diagnostics
from .queue import OfflineQueue

LOGGER = logging.getLogger("dayfinch-agent")


class TrackerAgent:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.activity = ActivityMonitor()
        self.queue = OfflineQueue(config.queue_dir, config.max_queue_items)
        self.client = TrackerClient(config.server_url, config.device_token, __version__)
        self.stop_event = threading.Event()
        self.capture_event = threading.Event()
        self._state_lock = threading.Lock()
        self._paused = False
        self._status = "Starting"
        self._worker: threading.Thread | None = None
        self._active_app = "Unknown"
        self._session_id = ""
        self._heartbeat_event = threading.Event()

    @property
    def paused(self) -> bool:
        with self._state_lock:
            return self._paused

    @property
    def status(self) -> str:
        with self._state_lock:
            return self._status

    def start(self) -> None:
        if not self.activity.start():
            LOGGER.warning(
                "Aggregate keyboard and mouse counts are unavailable on this desktop"
            )
        self._worker = threading.Thread(
            target=self._work_loop, name="tracker-worker", daemon=True
        )
        self._worker.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.capture_event.set()
        self.activity.stop()
        if self._worker and self._worker is not threading.current_thread():
            self._worker.join(timeout=8)
        try:
            self.client.heartbeat("stopped")
        except (httpx.HTTPError, OSError):
            pass
        self.client.close()

    def toggle_pause(self) -> None:
        with self._state_lock:
            self._paused = not self._paused
            paused = self._paused
            self._status = "Paused by employee" if paused else "Active"
        self.activity.set_enabled(not paused)
        self._heartbeat_event.set()
        LOGGER.info("Tracking %s", "paused" if paused else "resumed")

    def capture_now(self) -> None:
        if not self.paused:
            self.capture_event.set()

    def _set_status(self, value: str) -> None:
        with self._state_lock:
            self._status = value

    def _work_loop(self) -> None:
        next_capture = time.monotonic() + self.config.capture_interval_seconds
        next_heartbeat = 0.0
        next_upload = 0.0
        next_observation = 0.0
        while not self.stop_event.is_set():
            now = time.monotonic()
            if not self.paused and now >= next_observation:
                self._active_app = active_application()
                self.activity.observe(self._active_app, now=now)
                next_observation = now + 5.0
            if now >= next_heartbeat or self._heartbeat_event.is_set():
                self._heartbeat_event.clear()
                self._send_heartbeat()
                next_heartbeat = now + self.config.heartbeat_interval_seconds

            if not self.paused and (now >= next_capture or self.capture_event.is_set()):
                self.capture_event.clear()
                self._capture_to_queue()
                next_capture = time.monotonic() + self.config.capture_interval_seconds

            if now >= next_upload:
                uploaded = self._upload_one()
                next_upload = now + (0.2 if uploaded else 5.0)

            self.stop_event.wait(0.5)

    def _capture_to_queue(self) -> None:
        try:
            self._set_status("Capturing screenshot")
            screenshot = capture_screenshot(
                all_monitors=self.config.capture_all_monitors,
                jpeg_quality=self.config.jpeg_quality,
            )
            activity = self.activity.snapshot_and_reset()
            self.queue.add(
                screenshot, activity, self._active_app, session_id=self._session_id
            )
            self._set_status(f"Queued ({self.queue.count()} pending)")
            LOGGER.info("Screenshot captured and queued")
        except Exception:
            self._set_status("Capture failed")
            LOGGER.exception("Unable to capture screenshot")

    def _upload_one(self) -> bool:
        pending = self.queue.pending(limit=1)
        if not pending:
            if not self.paused:
                self._set_status("Active")
            return False
        record = pending[0]
        try:
            self._set_status(f"Uploading ({self.queue.count()} pending)")
            self.client.upload(record)
            self.queue.acknowledge(record)
            self._set_status("Active" if not self.paused else "Paused by employee")
            return True
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                self._set_status("Enrollment token revoked")
            else:
                self._set_status(f"Server rejected upload ({exc.response.status_code})")
            LOGGER.warning("Upload rejected: %s", exc)
        except (httpx.HTTPError, OSError) as exc:
            self._set_status(f"Offline ({self.queue.count()} pending)")
            LOGGER.debug("Upload deferred: %s", exc)
        return False

    def _send_heartbeat(self) -> None:
        try:
            self._session_id = self.client.heartbeat(
                "paused" if self.paused else "active", self.config.task_id
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                self._set_status("Enrollment token revoked")
        except (httpx.HTTPError, OSError):
            if self.queue.count():
                self._set_status(f"Offline ({self.queue.count()} pending)")


def _tray_image() -> Image.Image:
    image = Image.new("RGB", (64, 64), "#102c27")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((14, 11, 50, 53), radius=8, fill="#eaf4ef")
    draw.ellipse((24, 22, 40, 38), fill="#176b57")
    draw.rectangle((29, 39, 35, 48), fill="#176b57")
    return image


def run_tray(agent: TrackerAgent) -> None:
    import pystray

    icon: pystray.Icon

    def pause_label(_item: object) -> str:
        return "Resume tracking" if agent.paused else "Pause tracking"

    def status_label(_item: object) -> str:
        return f"Status: {agent.status}"

    def toggle(_icon: object, _item: object) -> None:
        agent.toggle_pause()
        icon.update_menu()

    def capture(_icon: object, _item: object) -> None:
        agent.capture_now()

    def quit_agent(_icon: object, _item: object) -> None:
        agent.stop()
        icon.stop()

    icon = pystray.Icon(
        "dayfinch",
        _tray_image(),
        "Dayfinch — visible and controllable",
        menu=pystray.Menu(
            pystray.MenuItem(status_label, None, enabled=False),
            pystray.MenuItem(pause_label, toggle),
            pystray.MenuItem(
                "Capture now", capture, enabled=lambda _item: not agent.paused
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit tracker", quit_agent),
        ),
    )
    icon.run()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Visible employee activity tracker agent"
    )
    parser.add_argument("--config", type=Path, default=Path("agent.toml"))
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Check OS support and permissions, print results, and exit",
    )
    parser.add_argument(
        "--capture-test",
        action="store_true",
        help="Capture one screenshot in memory to verify permission, then exit",
    )
    parser.add_argument(
        "--no-tray",
        action="store_true",
        help="Run visibly in this terminal (useful on Linux without a tray)",
    )
    return parser


def run() -> None:
    args = _parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    checks = run_diagnostics()
    if args.diagnose:
        print(format_diagnostics(checks))
        raise SystemExit(1 if has_failures(checks) else 0)
    if has_failures(checks):
        print(format_diagnostics(checks), file=sys.stderr)
        print(
            "Agent startup stopped because a required capability is unavailable.",
            file=sys.stderr,
        )
        raise SystemExit(3)
    if args.capture_test:
        try:
            screenshot = capture_screenshot(all_monitors=False, jpeg_quality=65)
            with Image.open(BytesIO(screenshot)) as image:
                image.verify()
            print(
                "Capture test passed; the screenshot was verified in memory and discarded."
            )
            return
        except Exception as exc:
            print(f"Capture test failed: {exc}", file=sys.stderr)
            raise SystemExit(4) from exc
    for check in checks:
        if check.status == "warn":
            LOGGER.warning("%s: %s", check.name, check.message)
    try:
        config = AgentConfig.from_file(args.config.resolve())
    except (OSError, ValueError) as exc:
        print(f"Agent configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    print(
        "Dayfinch is active and visible. It captures screenshots and aggregate "
        "keyboard/mouse counts; it never records typed text.",
        flush=True,
    )
    agent = TrackerAgent(config)
    try:
        agent.start()
        if args.no_tray:
            print("Press Ctrl+C to stop. Pause/resume is available with the tray.")
            while not agent.stop_event.wait(1):
                pass
        else:
            run_tray(agent)
    except KeyboardInterrupt:
        pass
    finally:
        if not agent.stop_event.is_set():
            agent.stop()


if __name__ == "__main__":
    run()

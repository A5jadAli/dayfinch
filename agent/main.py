from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

from . import __version__
from .active_app import active_application, active_website
from .activity import ActivityMonitor
from .capture import capture_screenshot
from .client import TrackerClient
from .config import AgentConfig
from .diagnostics import format_diagnostics, has_failures, run_diagnostics
from .idle import SystemIdleMonitor
from .queue import OfflineQueue
from .website_bridge import WebsiteBridge

LOGGER = logging.getLogger("dayfinch-agent")

# Consecutive 401s before the agent gives up. A revoked token never recovers, but
# a couple of retries keep a brief server-side blip from stopping tracking.
MAX_REJECTIONS = 3

# Foreground checks spawn a helper process on X11 and macOS, so they are the
# agent's main idle cost. This stays below ActivityMonitor's 15s observation gap,
# which keeps focus seconds accurate while halving those spawns.
OBSERVATION_INTERVAL_SECONDS = 10.0

# Retry delay when the queue has items the server would not take yet.
UPLOAD_RETRY_SECONDS = 5.0


class TrackerAgent:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.activity = ActivityMonitor()
        self.system_idle = SystemIdleMonitor()
        self.website_bridge = WebsiteBridge(
            config.website_bridge_token, config.website_bridge_port
        )
        self.queue = OfflineQueue(config.queue_dir, config.max_queue_items)
        self.client = TrackerClient(config.server_url, config.device_token, __version__)
        self.stop_event = threading.Event()
        self.capture_event = threading.Event()
        self._state_lock = threading.Lock()
        self._paused = False
        self._status = "Starting"
        self._worker: threading.Thread | None = None
        self._active_app = "Unknown"
        self._active_url = ""
        self._session_id = ""
        self._heartbeat_event = threading.Event()
        self._stopped = False
        self._rejections = 0
        self.revoked = False
        self._auto_idle = False
        self._idle_deduction_seconds = 0

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
        if (
            self.config.collect_websites
            and self.config.website_bridge_token
            and not self.website_bridge.start()
        ):
            LOGGER.warning(
                "The browser-domain bridge could not bind to 127.0.0.1:%s",
                self.config.website_bridge_port,
            )
        self._worker = threading.Thread(
            target=self._work_loop, name="tracker-worker", daemon=True
        )
        self._worker.start()

    def stop(self) -> None:
        """Idempotent: the tray, a signal, and the exit path may all call it."""
        with self._state_lock:
            if self._stopped:
                return
            self._stopped = True
        self.stop_event.set()
        self.capture_event.set()
        self.activity.stop()
        self.website_bridge.stop()
        if self._worker and self._worker is not threading.current_thread():
            self._worker.join(timeout=8)
        # Journal first. If the network or power disappears during shutdown, this
        # transition is replayed on the next launch instead of being lost.
        self._send_heartbeat(status="stopped")
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

    def _suspended(self) -> bool:
        """Tracking is not accruing time: the employee paused, or the desk is idle."""
        return self.paused or self._auto_idle

    def _update_idle_state(self, now: float) -> None:
        """Stop counting after a long spell with no input, and resume on return.

        Idle time is genuinely deducted because the work segment is closed while
        idle. This only runs where input can actually be observed; on Wayland the
        signal is unavailable, so absence of input is never assumed to be idleness.
        """
        timeout = self.config.idle_timeout_seconds
        if not timeout or self.paused:
            return
        idle = self.system_idle.seconds(now)
        if idle is None:
            idle = self.activity.seconds_since_input(now)
        if idle is None:
            return
        if not self._auto_idle and idle >= timeout:
            self._auto_idle = True
            # Closing at the last real input, rather than "now", removes the full
            # idle stretch that triggered the threshold.
            self._idle_deduction_seconds = round(idle)
            self._set_status("Idle — time not counted")
            LOGGER.info("No input for %.0f min; tracked time paused", idle / 60)
            self._heartbeat_event.set()
        elif self._auto_idle and idle < timeout:
            self._auto_idle = False
            self._set_status("Active")
            LOGGER.info("Input resumed; tracked time continues")
            self._heartbeat_event.set()

    def _note_rejection(self) -> None:
        """A 401 means the enrollment token is gone for good, so say so and stop.

        Retrying forever looks identical to a working agent from the desktop, and
        nothing can be uploaded until the device is enrolled again.
        """
        self._set_status("Enrollment token revoked")
        self._rejections += 1
        if self._rejections < MAX_REJECTIONS:
            LOGGER.warning(
                "Server rejected this device token (%s/%s). "
                "It was probably revoked in the dashboard.",
                self._rejections,
                MAX_REJECTIONS,
            )
            return
        if not self.revoked:
            self.revoked = True
            LOGGER.error(
                "This device's enrollment token is no longer valid. Tracking has "
                "stopped. Enroll the device again to get a new token."
            )
        self.stop_event.set()

    def _note_accepted(self) -> None:
        self._rejections = 0

    def _work_loop(self) -> None:
        next_capture = time.monotonic() + self.config.capture_interval_seconds
        next_heartbeat = 0.0
        next_upload = 0.0
        next_observation = 0.0
        while not self.stop_event.is_set():
            now = time.monotonic()
            self._update_idle_state(now)
            if not self._suspended() and now >= next_observation:
                self._active_app = active_application()
                self.activity.observe(self._active_app, now=now)
                next_observation = now + OBSERVATION_INTERVAL_SECONDS
            if now >= next_heartbeat or self._heartbeat_event.is_set():
                self._heartbeat_event.clear()
                self._send_heartbeat()
                next_heartbeat = now + self.config.heartbeat_interval_seconds

            if not self._suspended() and (
                now >= next_capture or self.capture_event.is_set()
            ):
                self.capture_event.clear()
                self._capture_to_queue()
                next_capture = time.monotonic() + self.config.capture_interval_seconds

            # count() is cached, so an empty queue costs nothing. Without this the
            # agent woke SQLite every few seconds for its whole idle life.
            if now >= next_upload and (self.queue.state_count() or self.queue.count()):
                # State must reach the server first so an offline screenshot can be
                # attributed to the reconstructed session containing captured_at.
                uploaded = (
                    self._upload_state_one()
                    if self.queue.state_count()
                    else self._upload_one()
                )
                next_upload = now + (0.2 if uploaded else UPLOAD_RETRY_SECONDS)

            self.stop_event.wait(0.5)

    def _capture_to_queue(self) -> None:
        try:
            self._set_status("Capturing screenshot")
            screenshot = capture_screenshot(
                all_monitors=self.config.capture_all_monitors,
                jpeg_quality=self.config.jpeg_quality,
                max_dimension=self.config.max_image_dimension,
            )
            if self.config.collect_websites:
                # The extension itself reports an empty domain when its browser
                # loses OS focus, which also makes this work on Wayland where the
                # desktop cannot identify another process's foreground window.
                self._active_url = (
                    self.website_bridge.current_domain()
                    or active_website(self._active_app)
                )
            activity = self.activity.snapshot_and_reset()
            self.queue.add(
                screenshot,
                activity,
                self._active_app,
                session_id=self._session_id,
                active_url=self._active_url,
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
                self._note_rejection()
            else:
                self._set_status(f"Server rejected upload ({exc.response.status_code})")
                LOGGER.warning("Upload rejected: %s", exc)
        except (httpx.HTTPError, OSError) as exc:
            self._set_status(f"Offline ({self.queue.count()} pending)")
            LOGGER.debug("Upload deferred: %s", exc)
        return False

    def _send_heartbeat(self, status: str | None = None) -> None:
        state = status or ("paused" if self._suspended() else "active")
        idle_seconds = self._idle_deduction_seconds if state == "paused" else 0
        self.queue.add_state(
            state,
            task_id=self.config.task_id if state != "stopped" else "",
            idle_seconds=idle_seconds,
            heartbeat_interval_seconds=self.config.heartbeat_interval_seconds,
        )
        self._idle_deduction_seconds = 0
        self._upload_state_one()

    def _upload_state_one(self) -> bool:
        pending = self.queue.pending_states(limit=1)
        if not pending:
            return False
        event = pending[0]
        try:
            self._session_id = self.client.heartbeat(event)
            self.queue.acknowledge_state(event)
            self._note_accepted()
            return True
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                self._note_rejection()
            else:
                LOGGER.warning(
                    "Heartbeat rejected by the server (%s)", exc.response.status_code
                )
        except (httpx.HTTPError, OSError):
            pending_count = self.queue.state_count() + self.queue.count()
            self._set_status(f"Offline ({pending_count} pending)")
        return False


def install_shutdown_handlers(agent: TrackerAgent) -> list[int]:
    """Close the work session on an orderly shutdown, not just on Ctrl+C.

    Service managers, desktop logout, and terminal close all terminate the agent
    with a signal rather than KeyboardInterrupt. Without this the session stays
    open forever and the employee cannot submit that period's timesheet.
    """
    installed: list[int] = []

    def request_stop(signal_number: int, _frame: object) -> None:
        LOGGER.info("Received signal %s; stopping tracking", signal_number)
        agent.stop_event.set()

    for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK"):
        handled = getattr(signal, name, None)
        if handled is None:
            continue
        try:
            signal.signal(handled, request_stop)
        except (OSError, ValueError):
            # Not every platform delivers every signal to the main thread.
            continue
        installed.append(int(handled))
    return installed


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

    def close_on_shutdown() -> None:
        agent.stop_event.wait()
        try:
            icon.stop()
        except Exception:  # pragma: no cover - backend teardown is best effort
            LOGGER.debug("Tray icon was already closed")

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
    threading.Thread(
        target=close_on_shutdown, name="tray-shutdown", daemon=True
    ).start()
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
        "Dayfinch is active and visible. It captures screenshots, aggregate "
        "keyboard/mouse counts, the foreground application, and the current "
        "website's domain; it never records typed text, full page URLs, or window "
        "titles. Long idle periods are not counted as work.",
        flush=True,
    )
    agent = TrackerAgent(config)
    install_shutdown_handlers(agent)
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
        # Always stop: the session must be closed even when a signal or the tray
        # already set stop_event. stop() is idempotent.
        agent.stop()
    if agent.revoked:
        print(
            "Tracking stopped: this device's enrollment token is no longer valid. "
            "Enroll the device again in the dashboard to get a new token.",
            file=sys.stderr,
        )
        raise SystemExit(5)


if __name__ == "__main__":
    run()

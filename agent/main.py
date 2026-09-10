from __future__ import annotations

import argparse
import json
import logging
import random
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

from . import __version__
from .active_app import active_application, active_website
from .activity import ActivityMonitor
from .automatic import active_window
from .capture import blur_screenshot, capture_screenshot, is_wayland
from .client import TrackerClient
from .config import AgentConfig
from .diagnostics import format_diagnostics, has_failures, run_diagnostics
from .idle import SystemIdleMonitor
from .installer import (
    InstallError,
    apply_install_plan,
    installed_in_macos_app_bundle,
    launch_install_helper,
    reconcile_install_plans,
)
from .onboarding import (
    ConfigurationImportError,
    default_config_path,
    install_configuration,
    prompt_for_configuration,
)
from .queue import OfflineQueue
from .reminders import ReminderSettings, active_reminder_window, reminder_is_due
from .update import UpdateClient, UpdateError
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
POLICY_REFRESH_SECONDS = 300.0
AUTOMATIC_SUPPRESSION_STATE = "automatic_suppressed_window"
PROJECT_CATALOG_STATE = "project_catalog"
REMINDER_STATE = "tracking_reminders"
LEGACY_REMINDER_SETTINGS_STATE = "tracking_reminder_settings"
LEGACY_REMINDER_LAST_SENT_STATE = "tracking_reminder_last_sent"
WAYLAND_RESTORE_TOKEN_STATE = "wayland_screencast_restore_token"


class TrackerAgent:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.activity = ActivityMonitor()
        self.system_idle = SystemIdleMonitor()
        self.website_bridge = WebsiteBridge(
            config.website_bridge_token, config.website_bridge_port
        )
        self.queue = OfflineQueue(
            config.queue_dir, config.max_queue_items, config.device_token
        )
        self.client = TrackerClient(config.server_url, config.device_token, __version__)
        self.stop_event = threading.Event()
        self.capture_event = threading.Event()
        self._state_lock = threading.Lock()
        self._reminder_lock = threading.Lock()
        self._timer_state = "stopped"
        self._status = "Not tracking"
        self._worker: threading.Thread | None = None
        self._update_thread: threading.Thread | None = None
        self._active_app = "Unknown"
        self._active_url = ""
        self._session_id = ""
        self._selected_task_id = config.task_id
        self._selected_project_id = config.project_id
        self._note = ""
        self._capture_interval_seconds = config.capture_interval_seconds
        self._screenshots_enabled = True
        self._screenshot_blur = False
        self._track_apps = True
        self._track_urls = config.collect_websites
        self._idle_timeout_seconds = config.idle_timeout_seconds
        self._heartbeat_event = threading.Event()
        self._stopped = False
        self._rejections = 0
        self.revoked = False
        self._auto_idle = False
        self._idle_deduction_seconds = 0
        self._automatic_policy: dict | None = None
        self._automatic_window_key = ""
        self._automatic_started = False
        self._automatic_suppressed_window_key = self.queue.local_state(
            AUTOMATIC_SUPPRESSION_STATE
        )
        try:
            encoded_reminders = self.queue.local_state(REMINDER_STATE)
            if encoded_reminders:
                reminder_state = json.loads(encoded_reminders)
            else:
                # Read the brief pre-0.6 development format so local testers do
                # not lose a preference when upgrading to the atomic record.
                reminder_state = {
                    "settings": json.loads(
                        self.queue.local_state(LEGACY_REMINDER_SETTINGS_STATE) or "{}"
                    ),
                    "last_sent_at": self.queue.local_state(
                        LEGACY_REMINDER_LAST_SENT_STATE
                    ),
                }
        except (json.JSONDecodeError, OSError, sqlite3.Error, ValueError):
            LOGGER.exception("Encrypted tracking-reminder state is unavailable")
            reminder_state = {}
        if not isinstance(reminder_state, dict):
            reminder_state = {}
        self._reminder_settings = ReminderSettings.from_mapping(
            reminder_state.get("settings")
        )
        try:
            saved_reminder_time = reminder_state.get("last_sent_at", "")
            self._reminder_last_sent_at = (
                datetime.fromisoformat(saved_reminder_time)
                if isinstance(saved_reminder_time, str) and saved_reminder_time
                else None
            )
        except (TypeError, ValueError):
            LOGGER.warning("Invalid tracking-reminder history was ignored")
            self._reminder_last_sent_at = None
        try:
            cached_projects = json.loads(
                self.queue.local_state(PROJECT_CATALOG_STATE) or "[]"
            )
        except (json.JSONDecodeError, OSError, sqlite3.Error, ValueError):
            LOGGER.exception("Encrypted offline project catalog is unavailable")
            cached_projects = []
        self._projects = self._sanitize_projects(cached_projects)
        self.website_bridge.set_timer_controller(self)

    @property
    def paused(self) -> bool:
        with self._state_lock:
            return self._timer_state == "paused"

    @property
    def timer_state(self) -> str:
        with self._state_lock:
            return self._timer_state

    @property
    def status(self) -> str:
        with self._state_lock:
            return self._status

    @property
    def tracking_active(self) -> bool:
        return self.timer_state == "active" and not self._auto_idle

    def start(self) -> None:
        try:
            self.catalog()
        except (httpx.HTTPError, OSError, ValueError):
            LOGGER.warning("Using local tracking policy until the server is reachable")
        if not self.activity.start():
            LOGGER.warning(
                "Aggregate keyboard and mouse counts are unavailable on this desktop"
            )
        # Keep the listeners ready, but discard input until this standard timer is
        # explicitly started by the employee.
        self.activity.set_enabled(False)
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
        # A prior crash may have left a server session open. Close it without
        # counting time merely because the application was opened again.
        self._heartbeat_event.set()
        if self.config.update_manifest_url and self.config.update_mode != "off":
            self._update_thread = threading.Thread(
                target=self._check_for_update,
                name="update-check",
                daemon=True,
            )
            self._update_thread.start()

    def _check_for_update(self) -> None:
        try:
            with UpdateClient(
                self.config.update_manifest_url,
                self.config.update_public_key,
                __version__,
                self.config.queue_dir.parent / "updates",
            ) as updater:
                update = updater.check()
                if update is None:
                    LOGGER.info("Desktop agent is up to date")
                    return
                if installed_in_macos_app_bundle(Path(sys.executable)):
                    LOGGER.warning(
                        "Dayfinch %s is available. Install the signed and notarized "
                        "macOS .pkg from your organization's tracker download page; "
                        "the app bundle will not be modified in place.",
                        update.version,
                    )
                    return
                if self.config.update_mode == "download":
                    path = updater.download(update)
                    LOGGER.warning(
                        "Dayfinch %s is verified and ready at %s. Stop tracking, "
                        "then run --install-update from the packaged agent.",
                        update.version,
                        path,
                    )
                else:
                    LOGGER.warning(
                        "Dayfinch %s is available. Run with --install-update to "
                        "verify, replace, diagnose, and roll back safely if needed.",
                        update.version,
                    )
        except (httpx.HTTPError, OSError, UpdateError) as exc:
            LOGGER.warning("Desktop update check failed safely: %s", exc)

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
        try:
            self._send_heartbeat(status="stopped")
        except (OSError, sqlite3.Error, ValueError) as exc:
            LOGGER.error("Unable to journal the final stopped event: %s", exc)
        finally:
            self.client.close()

    def toggle_pause(self) -> None:
        if self.timer_state == "active":
            self.pause_tracking()
        elif self.timer_state == "paused":
            self.resume_tracking()
        else:
            raise ValueError("Start the timer before pausing it")

    def start_tracking(self) -> None:
        with self._state_lock:
            if self._timer_state != "stopped":
                return
            if not self._selected_project_id:
                raise ValueError("Choose a project before starting the timer")
            self._timer_state = "active"
            self._status = "Active"
            self._auto_idle = False
        self.activity.set_enabled(True)
        self._heartbeat_event.set()
        LOGGER.info("Tracking started")

    def pause_tracking(self) -> None:
        with self._state_lock:
            if self._timer_state != "active":
                raise ValueError("Only an active timer can be paused")
            self._timer_state = "paused"
            self._status = "Paused by employee"
        self.activity.set_enabled(False)
        self._heartbeat_event.set()
        LOGGER.info("Tracking paused")

    def resume_tracking(self) -> None:
        with self._state_lock:
            if self._timer_state != "paused":
                raise ValueError("Only a paused timer can be resumed")
            self._timer_state = "active"
            self._status = "Active"
            self._auto_idle = False
        self.activity.set_enabled(True)
        self._heartbeat_event.set()
        LOGGER.info("Tracking resumed")

    def stop_tracking(self, *, automatic: bool = False) -> None:
        suppressed_key = ""
        with self._state_lock:
            if self._timer_state == "stopped":
                return
            self._timer_state = "stopped"
            self._status = "Not tracking"
            self._auto_idle = False
            if not automatic:
                # Keep the current window key so a deliberate employee stop is not
                # undone half a second later by the same schedule window.
                self._automatic_started = False
                suppressed_key = self._automatic_window_key
                if suppressed_key:
                    self._automatic_suppressed_window_key = suppressed_key
        self.activity.set_enabled(False)
        self.defer_tracking_reminder()
        if suppressed_key:
            try:
                self.queue.set_local_state(AUTOMATIC_SUPPRESSION_STATE, suppressed_key)
            except (OSError, sqlite3.Error, ValueError):
                LOGGER.exception("Could not persist automatic-window suppression")
        self._heartbeat_event.set()
        LOGGER.info("Tracking stopped")

    @property
    def selected_task_id(self) -> str:
        with self._state_lock:
            return self._selected_task_id

    @property
    def selected_project_id(self) -> str:
        with self._state_lock:
            return self._selected_project_id

    def select_work(self, project_id: str, task_id: str = "") -> None:
        with self._state_lock:
            changed = (project_id, task_id) != (
                self._selected_project_id,
                self._selected_task_id,
            )
            self._selected_project_id = project_id
            self._selected_task_id = task_id
        if changed:
            self._heartbeat_event.set()

    def set_note(self, note: str) -> None:
        with self._state_lock:
            self._note = note.strip()[:500]
        self._heartbeat_event.set()

    def catalog(self) -> dict:
        policy = self.client.configuration()
        frequency = int(policy.get("screenshot_frequency", 1))
        self._screenshots_enabled = frequency > 0
        self._capture_interval_seconds = (
            86_400 if frequency <= 0 else max(60, 600 // frequency)
        )
        self._screenshot_blur = bool(policy.get("screenshot_blur"))
        self._track_apps = bool(policy.get("track_apps", True))
        self._track_urls = bool(policy.get("track_urls", True))
        self._idle_timeout_seconds = max(
            60, int(policy.get("idle_timeout_minutes", 20)) * 60
        )
        projects = self._sanitize_projects(policy.get("projects", []))
        with self._state_lock:
            self._projects = projects
        try:
            self.queue.set_local_state(
                PROJECT_CATALOG_STATE,
                json.dumps(projects, sort_keys=True, separators=(",", ":")),
            )
        except (OSError, sqlite3.Error, ValueError):
            LOGGER.exception("Could not persist the encrypted offline project catalog")
        automatic = policy.get("automatic_tracking")
        with self._state_lock:
            previous_id = (self._automatic_policy or {}).get("id")
            previous_started = self._automatic_started
            self._automatic_policy = automatic if isinstance(automatic, dict) else None
            current_id = (self._automatic_policy or {}).get("id")
            policy_changed = previous_id is not None and previous_id != current_id
            if policy_changed:
                self._automatic_window_key = ""
                self._automatic_suppressed_window_key = ""
        if policy_changed:
            try:
                self.queue.set_local_state(AUTOMATIC_SUPPRESSION_STATE, "")
            except (OSError, sqlite3.Error, ValueError):
                LOGGER.exception(
                    "Could not clear obsolete automatic-window suppression"
                )
        if policy_changed and previous_started and self.timer_state != "stopped":
            self.stop_tracking(automatic=True)
            with self._state_lock:
                self._automatic_started = False
            self._set_status("Not tracking · automatic policy changed")
        return policy

    @staticmethod
    def _sanitize_projects(value: object) -> list[dict]:
        if not isinstance(value, list):
            return []
        projects: list[dict] = []
        for project in value[:500]:
            if not isinstance(project, dict):
                continue
            project_id = str(project.get("id", ""))[:80]
            name = str(project.get("name", ""))[:120]
            if not project_id or not name:
                continue
            tasks = []
            raw_tasks = project.get("tasks", [])
            if isinstance(raw_tasks, list):
                for task in raw_tasks[:500]:
                    if not isinstance(task, dict):
                        continue
                    task_id = str(task.get("id", ""))[:80]
                    task_name = str(task.get("name", ""))[:120]
                    if task_id and task_name:
                        tasks.append({"id": task_id, "name": task_name})
            projects.append({"id": project_id, "name": name, "tasks": tasks})
        return projects

    def browser_timer_snapshot(self) -> dict:
        reminder_settings = self.tracking_reminder_settings
        with self._state_lock:
            projects = [
                {
                    "id": str(project.get("id", "")),
                    "name": str(project.get("name", ""))[:120],
                    "tasks": [
                        {
                            "id": str(task.get("id", "")),
                            "name": str(task.get("name", ""))[:120],
                        }
                        for task in project.get("tasks", [])
                        if isinstance(task, dict)
                    ],
                }
                for project in self._projects
            ]
            return {
                "state": self._timer_state,
                "status": self._status,
                "project_id": self._selected_project_id,
                "task_id": self._selected_task_id,
                "projects": projects,
                "reminder_settings": reminder_settings,
            }

    @property
    def tracking_reminder_settings(self) -> dict:
        with self._reminder_lock:
            return self._reminder_settings.as_dict()

    def configure_tracking_reminders(self, values: object) -> dict:
        settings = ReminderSettings.from_mapping(values, strict=True)
        # Persist before publishing the new preference in memory. A full/damaged
        # journal must never make a setting appear saved when it will vanish.
        with self._reminder_lock:
            self._persist_reminder_state(settings, None)
            self._reminder_settings = settings
            self._reminder_last_sent_at = None
        return settings.as_dict()

    def _persist_reminder_state(
        self, settings: ReminderSettings, sent_at: datetime | None
    ) -> None:
        encoded = json.dumps(
            {
                "settings": settings.as_dict(),
                "last_sent_at": sent_at.isoformat() if sent_at else "",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        self.queue.set_local_state(REMINDER_STATE, encoded)

    def defer_tracking_reminder(self, moment: datetime | None = None) -> None:
        observed = moment or datetime.now().astimezone()
        with self._reminder_lock:
            settings = self._reminder_settings
            if active_reminder_window(settings, observed) is None:
                return
            sent_at = observed.astimezone(UTC)
            self._reminder_last_sent_at = sent_at
            try:
                self._persist_reminder_state(settings, sent_at)
            except (OSError, sqlite3.Error, ValueError):
                LOGGER.exception("Could not persist the tracking-reminder deferral")

    def tracking_reminder(self, moment: datetime | None = None) -> dict | None:
        observed = moment or datetime.now().astimezone()
        if self.timer_state != "stopped":
            return None
        with self._reminder_lock:
            # Check again after taking the reminder lock so a simultaneous Start
            # cannot race a reminder delivery.
            if self.timer_state != "stopped":
                return None
            settings = self._reminder_settings
            last_sent_at = self._reminder_last_sent_at
            window = reminder_is_due(settings, observed, last_sent_at)
            if window is None:
                return None
            sent_at = observed.astimezone(UTC)
            self._reminder_last_sent_at = sent_at
            try:
                self._persist_reminder_state(settings, sent_at)
            except (OSError, sqlite3.Error, ValueError):
                # Memory is already advanced so a damaged journal cannot cause a
                # notification storm every half-second.
                LOGGER.exception("Could not persist tracking-reminder delivery")
        remaining = max(0, int((window.ends_at - observed).total_seconds()))
        hours, remainder = divmod(remaining, 3600)
        minutes = remainder // 60
        remaining_text = f"{hours}h {minutes}m" if hours else f"{max(1, minutes)}m"
        return {
            "mode": settings.mode,
            "message": (
                "You are not tracking time. "
                f"The reminder window has {remaining_text} remaining."
            ),
            "window_ends_at": window.ends_at.isoformat(),
        }

    def browser_timer_action(
        self, action: str, project_id: str, task_id: str, note: str
    ) -> dict:
        if action == "start":
            with self._state_lock:
                project = next(
                    (
                        item
                        for item in self._projects
                        if str(item.get("id", "")) == project_id
                    ),
                    None,
                )
            if not project:
                raise ValueError("Choose an assigned project")
            task_ids = {
                str(task.get("id", ""))
                for task in project.get("tasks", [])
                if isinstance(task, dict)
            }
            if task_id and task_id not in task_ids:
                raise ValueError("Choose a task in the selected project")
            self.select_work(project_id, task_id)
            self.set_note(note)
            self.start_tracking()
        elif action == "pause":
            self.pause_tracking()
        elif action == "resume":
            self.resume_tracking()
        elif action == "stop":
            self.stop_tracking()
        else:
            raise ValueError("Unknown timer action")
        return self.browser_timer_snapshot()

    @property
    def pending_automatic_policy(self) -> dict | None:
        with self._state_lock:
            if (
                not self._automatic_policy
                or self._automatic_policy.get("consent_status") != "pending"
            ):
                return None
            return dict(self._automatic_policy)

    def respond_automatic_policy(self, policy_id: str, accepted: bool) -> None:
        self.client.automatic_tracking_consent(policy_id, accepted)
        with self._state_lock:
            if self._automatic_policy and self._automatic_policy.get("id") == policy_id:
                self._automatic_policy["consent_status"] = (
                    "accepted" if accepted else "declined"
                )
                self._automatic_window_key = ""
                self._automatic_started = False

    def _recent_activity(self, now: float) -> bool:
        signals = (
            self.system_idle.seconds(now),
            self.activity.seconds_since_input(now),
        )
        observed = [seconds for seconds in signals if seconds is not None]
        return bool(observed) and min(observed) <= 120

    def _apply_automatic_tracking(self, moment: datetime, now: float) -> None:
        with self._state_lock:
            policy = dict(self._automatic_policy) if self._automatic_policy else None
            automatic_started = self._automatic_started
            handled_key = self._automatic_window_key
            suppressed_key = self._automatic_suppressed_window_key
        window = active_window(policy, moment)
        if window is None:
            if automatic_started and self.timer_state != "stopped":
                self.stop_tracking(automatic=True)
                self._set_status("Not tracking · automatic schedule ended")
            with self._state_lock:
                self._automatic_window_key = ""
                self._automatic_started = False
                clear_suppression = bool(self._automatic_suppressed_window_key)
                self._automatic_suppressed_window_key = ""
            if clear_suppression:
                try:
                    self.queue.set_local_state(AUTOMATIC_SUPPRESSION_STATE, "")
                except (OSError, sqlite3.Error, ValueError):
                    LOGGER.exception("Could not clear expired automatic suppression")
            return
        if suppressed_key == window.key:
            self._set_status(
                "Not tracking · automatic start suppressed for this window"
            )
            return
        if handled_key == window.key:
            return
        if self.timer_state != "stopped":
            # A timer the employee started manually is never taken over or stopped
            # by a policy which happened to become active later.
            with self._state_lock:
                self._automatic_window_key = window.key
            return
        if (
            policy
            and policy.get("wait_for_activity")
            and not self._recent_activity(now)
        ):
            self._set_status("Automatic schedule waiting for activity")
            return
        self.select_work(window.project_id, "")
        self.start_tracking()
        with self._state_lock:
            self._automatic_window_key = window.key
            self._automatic_started = True
        self._set_status(
            f"Active · automatic policy: {policy.get('name', 'scheduled')}"
        )

    def capture_now(self) -> None:
        if self.tracking_active and self._screenshots_enabled:
            self.capture_event.set()

    def _set_status(self, value: str) -> None:
        with self._state_lock:
            self._status = value

    def _suspended(self) -> bool:
        """Tracking is not accruing time: the employee paused, or the desk is idle."""
        return not self.tracking_active

    def _update_idle_state(self, now: float) -> None:
        """Stop counting after a long spell with no input, and resume on return.

        Idle time is genuinely deducted because the work segment is closed while
        idle. This only runs where input can actually be observed; on Wayland the
        signal is unavailable, so absence of input is never assumed to be idleness.
        """
        timeout = self._idle_timeout_seconds
        if not timeout or self.timer_state != "active":
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
            self._set_status("Idle: time not counted")
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
        next_capture = (
            time.monotonic()
            + random.uniform(0.75, 1.25) * self._capture_interval_seconds
        )
        next_heartbeat = 0.0
        next_upload = 0.0
        next_observation = 0.0
        next_policy_refresh = time.monotonic() + POLICY_REFRESH_SECONDS
        while not self.stop_event.is_set():
            now = time.monotonic()
            if now >= next_policy_refresh:
                try:
                    self.catalog()
                    next_capture = (
                        now
                        + random.uniform(0.75, 1.25) * self._capture_interval_seconds
                    )
                except (httpx.HTTPError, OSError, ValueError):
                    LOGGER.debug("Tracking policy refresh deferred while offline")
                next_policy_refresh = now + POLICY_REFRESH_SECONDS
            self._apply_automatic_tracking(datetime.now().astimezone(), now)
            self._update_idle_state(now)
            if not self._suspended() and now >= next_observation:
                try:
                    self._active_app = active_application() if self._track_apps else ""
                    self._active_url = (
                        self.website_bridge.current_domain()
                        or active_website(self._active_app)
                        if self._track_urls
                        else ""
                    )
                    self.activity.observe(self._active_app, now=now)
                    self.queue.add_usage(
                        self._active_app,
                        self._active_url,
                        round(OBSERVATION_INTERVAL_SECONDS),
                    )
                except (OSError, sqlite3.Error, ValueError) as exc:
                    self._set_status("Local journal unavailable · tracking not saved")
                    LOGGER.error("Unable to journal application/domain usage: %s", exc)
                next_observation = now + OBSERVATION_INTERVAL_SECONDS
            if (
                self.timer_state != "stopped" and now >= next_heartbeat
            ) or self._heartbeat_event.is_set():
                transition = self._heartbeat_event.is_set()
                self._heartbeat_event.clear()
                try:
                    self._send_heartbeat(transition=transition)
                except (OSError, sqlite3.Error, ValueError) as exc:
                    self._set_status("Local journal unavailable · tracking not saved")
                    LOGGER.error("Unable to journal time heartbeat: %s", exc)
                next_heartbeat = now + self.config.heartbeat_interval_seconds

            if (
                self._screenshots_enabled
                and not self._suspended()
                and (now >= next_capture or self.capture_event.is_set())
            ):
                self.capture_event.clear()
                self._capture_to_queue()
                next_capture = (
                    time.monotonic()
                    + random.uniform(0.75, 1.25) * self._capture_interval_seconds
                )

            # count() is cached, so an empty queue costs nothing. Without this the
            # agent woke SQLite every few seconds for its whole idle life.
            if now >= next_upload and (
                self.queue.state_count()
                or self.queue.usage_count()
                or self.queue.count()
            ):
                # State must reach the server first so an offline screenshot can be
                # attributed to the reconstructed session containing captured_at.
                try:
                    uploaded = (
                        self._upload_state_one()
                        if self.queue.state_count()
                        else self._upload_usage_one()
                        if self.queue.usage_count()
                        else self._upload_one()
                    )
                except (OSError, sqlite3.Error, ValueError) as exc:
                    uploaded = False
                    self._set_status("Encrypted queue needs attention")
                    LOGGER.error("Unable to read the encrypted queue: %s", exc)
                next_upload = now + (0.2 if uploaded else UPLOAD_RETRY_SECONDS)

            self.stop_event.wait(0.5)

    def _capture_to_queue(self) -> None:
        try:
            self._set_status("Capturing screenshot")
            wayland_options = {}
            if is_wayland():
                wayland_options = {
                    "wayland_restore_token": self.queue.local_state(
                        WAYLAND_RESTORE_TOKEN_STATE
                    ),
                    "save_wayland_restore_token": lambda token: (
                        self.queue.set_local_state(WAYLAND_RESTORE_TOKEN_STATE, token)
                    ),
                }
            screenshot = capture_screenshot(
                all_monitors=self.config.capture_all_monitors,
                jpeg_quality=self.config.jpeg_quality,
                max_dimension=self.config.max_image_dimension,
                **wayland_options,
            )
            if self._screenshot_blur:
                screenshot = blur_screenshot(screenshot, self.config.jpeg_quality)
            if self._track_urls:
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
                screenshot_blurred=self._screenshot_blur,
            )
            self._set_status(f"Queued ({self.queue.count()} pending)")
            LOGGER.info("Screenshot captured and queued")
        except Exception:
            self._set_status("Capture failed")
            LOGGER.exception("Unable to capture screenshot")

    def _upload_one(self) -> bool:
        pending = self.queue.pending(limit=1)
        if not pending:
            self._set_status(self._timer_status())
            return False
        record = pending[0]
        try:
            self._set_status(f"Uploading ({self.queue.count()} pending)")
            screenshot = self.queue.read_screenshot(record)
            upload_record = record
            if self._screenshot_blur and not record.screenshot_blurred:
                screenshot = blur_screenshot(screenshot, self.config.jpeg_quality)
                upload_record = replace(record, screenshot_blurred=True)
            self.client.upload(upload_record, screenshot)
            self.queue.acknowledge(record)
            self._set_status(self._timer_status())
            return True
        except (ValueError, FileNotFoundError) as exc:
            self.queue.quarantine_record(record, str(exc))
            self._set_status(
                f"Needs attention · {self.queue.quarantine_count()} quarantined"
            )
            LOGGER.error("Queued screenshot quarantined: %s", exc)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                self._note_rejection()
            elif (
                exc.response.status_code == 403
                and "screenshot collection is disabled" in exc.response.text.lower()
            ):
                self.queue.acknowledge(record)
                self._screenshots_enabled = False
                self._set_status("Active · screenshots disabled by policy")
            elif (
                exc.response.status_code == 403
                and "screenshot blur is required" in exc.response.text.lower()
            ):
                # A policy may change while this machine is offline. Keep the
                # encrypted capture local and irreversibly blur it before retry.
                self._screenshot_blur = True
                self._set_status("Active · applying required screenshot blur")
            elif exc.response.status_code in {400, 404, 409, 410, 413, 415, 422}:
                self.queue.quarantine_record(record, exc.response.text)
                self._set_status(
                    f"Needs attention · {self.queue.quarantine_count()} quarantined"
                )
                LOGGER.error("Permanently rejected screenshot quarantined: %s", exc)
            else:
                self._set_status(f"Server rejected upload ({exc.response.status_code})")
                LOGGER.warning("Upload rejected: %s", exc)
        except (httpx.HTTPError, OSError) as exc:
            self._set_status(f"Offline ({self.queue.count()} pending)")
            LOGGER.debug("Upload deferred: %s", exc)
        return False

    def _send_heartbeat(
        self, status: str | None = None, *, transition: bool = True
    ) -> None:
        state = status or (
            "stopped"
            if self.timer_state == "stopped"
            else "paused"
            if self._suspended()
            else "active"
        )
        idle_seconds = self._idle_deduction_seconds if state == "paused" else 0
        self.queue.add_state(
            state,
            task_id=self.selected_task_id if state != "stopped" else "",
            project_id=self.selected_project_id if state != "stopped" else "",
            note=self._note if state != "stopped" else "",
            idle_seconds=idle_seconds,
            heartbeat_interval_seconds=self.config.heartbeat_interval_seconds,
            transition=transition,
        )
        self._idle_deduction_seconds = 0
        self._upload_state_one()

    def _timer_status(self) -> str:
        if self.timer_state == "stopped":
            return "Not tracking"
        if self.timer_state == "paused":
            return "Paused by employee"
        if self._auto_idle:
            return "Idle: time not counted"
        return "Active"

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
            elif exc.response.status_code in {400, 404, 409, 410, 413, 415, 422}:
                self.queue.quarantine_state(event, exc.response.text)
                current_transition_rejected = (
                    event.status in {"active", "paused"}
                    and event.project_id == self.selected_project_id
                    and self.queue.state_count() == 0
                    and self.timer_state != "stopped"
                )
                if current_transition_rejected:
                    self.stop_tracking()
                    self._set_status("Tracking stopped · server rejected this work")
                else:
                    self._set_status(
                        f"Needs attention · {self.queue.quarantine_count()} quarantined"
                    )
                LOGGER.error("Permanently rejected time event quarantined: %s", exc)
            else:
                LOGGER.warning(
                    "Heartbeat rejected by the server (%s)", exc.response.status_code
                )
        except (httpx.HTTPError, OSError):
            pending_count = (
                self.queue.state_count() + self.queue.usage_count() + self.queue.count()
            )
            self._set_status(f"Offline ({pending_count} pending)")
        return False

    def _upload_usage_one(self) -> bool:
        pending = self.queue.pending_usage(limit=1)
        if not pending:
            return False
        event = pending[0]
        try:
            self.client.upload_usage(event)
            self.queue.acknowledge_usage(event)
            self._note_accepted()
            return True
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                self._note_rejection()
            elif exc.response.status_code in {400, 404, 409, 410, 413, 415, 422}:
                self.queue.quarantine_usage(event, exc.response.text)
                self._set_status(
                    f"Needs attention · {self.queue.quarantine_count()} quarantined"
                )
                LOGGER.error("Permanently rejected usage event quarantined: %s", exc)
            else:
                LOGGER.warning(
                    "Usage sample rejected by the server (%s)", exc.response.status_code
                )
        except (httpx.HTTPError, OSError):
            pending_count = (
                self.queue.state_count() + self.queue.usage_count() + self.queue.count()
            )
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

    def timer_label(_item: object) -> str:
        return "Stop tracking" if agent.timer_state != "stopped" else "Start tracking"

    def status_label(_item: object) -> str:
        return f"Status: {agent.status}"

    def toggle(_icon: object, _item: object) -> None:
        agent.toggle_pause()
        icon.update_menu()

    def toggle_timer(_icon: object, _item: object) -> None:
        if agent.timer_state == "stopped":
            agent.start_tracking()
        else:
            agent.stop_tracking()
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
        "Dayfinch: visible and controllable",
        menu=pystray.Menu(
            pystray.MenuItem(status_label, None, enabled=False),
            pystray.MenuItem(timer_label, toggle_timer),
            pystray.MenuItem(
                pause_label,
                toggle,
                enabled=lambda _item: agent.timer_state != "stopped",
            ),
            pystray.MenuItem(
                "Capture now", capture, enabled=lambda _item: agent.tracking_active
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
    parser.add_argument(
        "--config",
        type=Path,
        help="Configuration path (packaged default: private per-user directory)",
    )
    parser.add_argument(
        "--import-config",
        type=Path,
        help="Validate and securely install an agent.toml enrollment file, then exit",
    )
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
    parser.add_argument(
        "--start",
        action="store_true",
        help="Explicitly start the configured project when the agent opens",
    )
    update = parser.add_mutually_exclusive_group()
    update.add_argument(
        "--check-update",
        action="store_true",
        help="Verify the signed release manifest, report an update, and exit",
    )
    update.add_argument(
        "--download-update",
        action="store_true",
        help="Verify and stage the latest release without executing it, then exit",
    )
    update.add_argument(
        "--install-update",
        action="store_true",
        help="Verify, stage, and safely install the latest packaged release",
    )
    parser.add_argument(
        "--apply-update-plan",
        type=Path,
        help=argparse.SUPPRESS,
    )
    return parser


def _load_config(path: Path) -> AgentConfig:
    try:
        return AgentConfig.from_file(path.resolve())
    except (OSError, ValueError) as exc:
        print(f"Agent configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _run_update_command(config: AgentConfig, *, mode: str) -> None:
    if not config.update_manifest_url:
        print("No authenticated update channel is configured.", file=sys.stderr)
        raise SystemExit(6)
    try:
        with UpdateClient(
            config.update_manifest_url,
            config.update_public_key,
            __version__,
            config.queue_dir.parent / "updates",
        ) as updater:
            update = updater.check()
            if update is None:
                print(f"Dayfinch {__version__} is up to date.")
                return
            if mode == "check":
                print(
                    f"Dayfinch {update.version} is available for "
                    f"{update.artifact.platform}."
                )
                return
            path = updater.download(update)
            if mode == "download":
                print(
                    f"Verified Dayfinch {update.version} and staged it at {path}. "
                    "Dayfinch did not execute or replace the current file."
                )
                return
            if not getattr(sys, "frozen", False):
                raise InstallError(
                    "Safe replacement is available only from a packaged Dayfinch executable"
                )
            plan = launch_install_helper(
                Path(sys.executable),
                path,
                config.queue_dir.parent / "updates",
                __version__,
                update.version,
            )
            print(
                f"Verified Dayfinch {update.version}. The detached installer will "
                f"replace this executable after it exits, run diagnostics, and roll "
                f"back automatically on failure. Recovery plan: {plan}"
            )
    except (httpx.HTTPError, OSError, UpdateError, InstallError) as exc:
        print(f"Update check failed safely: {exc}", file=sys.stderr)
        raise SystemExit(6) from exc


def run() -> None:
    args = _parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.apply_update_plan:
        try:
            apply_install_plan(args.apply_update_plan.resolve())
        except InstallError as exc:
            LOGGER.error("Desktop update failed safely: %s", exc)
            raise SystemExit(7) from exc
        return
    packaged = bool(getattr(sys, "frozen", False))
    config_path = args.config or (
        default_config_path() if packaged else Path("agent.toml")
    )
    if args.import_config:
        destination = args.config or default_config_path()
        try:
            installed = install_configuration(
                args.import_config.resolve(), destination, replace=True
            )
        except ConfigurationImportError as exc:
            print(f"Configuration import failed: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        print(f"Dayfinch configuration installed at {installed}")
        return
    needs_config = not (args.diagnose or args.capture_test)
    if packaged and needs_config and not config_path.exists() and not args.no_tray:
        try:
            config_path = prompt_for_configuration(config_path)
        except ConfigurationImportError as exc:
            print(f"Agent configuration error: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
    if args.check_update or args.download_update or args.install_update:
        mode = (
            "install"
            if args.install_update
            else "download"
            if args.download_update
            else "check"
        )
        _run_update_command(_load_config(config_path), mode=mode)
        return
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
    config = _load_config(config_path)
    if getattr(sys, "frozen", False):
        try:
            recovered = reconcile_install_plans(
                config.queue_dir.parent / "updates", Path(sys.executable)
            )
        except InstallError as exc:
            print(f"Update recovery stopped startup: {exc}", file=sys.stderr)
            raise SystemExit(7) from exc
        if recovered:
            LOGGER.info("Reconciled %s interrupted desktop update plan(s)", recovered)

    print(
        "Dayfinch is active and visible. It captures screenshots, aggregate "
        "keyboard/mouse counts, the foreground application, and the current "
        "website's domain; it never records typed text, full page URLs, or window "
        "titles. Long idle periods are not counted as work.",
        flush=True,
    )
    try:
        agent = TrackerAgent(config)
    except (OSError, ValueError) as exc:
        print(f"Offline queue error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    install_shutdown_handlers(agent)
    try:
        agent.start()
        if args.start:
            try:
                agent.start_tracking()
            except ValueError as exc:
                print(f"Cannot start timer: {exc}", file=sys.stderr)
                raise SystemExit(2) from exc
        if args.no_tray:
            print(
                "Press Ctrl+C to quit. Use --start with a configured project to "
                "track in terminal mode."
            )
            while not agent.stop_event.wait(1):
                pass
        else:
            from .desktop import run_desktop

            run_desktop(agent)
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

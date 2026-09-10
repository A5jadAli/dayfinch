import signal
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from io import BytesIO

import httpx
import pytest
from PIL import Image

from agent import capture
from agent import main as agent_main
from agent.config import AgentConfig
from agent.main import MAX_REJECTIONS, TrackerAgent, install_shutdown_handlers

PROJECT_ID = "11111111-1111-4111-8111-111111111111"


def _config(tmp_path, **overrides) -> AgentConfig:
    values = {
        "server_url": "http://127.0.0.1:8000",
        "device_token": "t" * 40,
        "consent_confirmed": True,
        "project_id": PROJECT_ID,
        "queue_dir": tmp_path / "queue",
    }
    values.update(overrides)
    return AgentConfig(**values)


class _Rejecting:
    """Stands in for a server that has revoked this device's token."""

    def __init__(self, status_code: int = 401):
        self.calls = 0
        self.status_code = status_code

    def heartbeat(self, _event) -> str:
        self.calls += 1
        request = httpx.Request("POST", "http://127.0.0.1:8000/api/v1/heartbeat")
        response = httpx.Response(self.status_code, request=request)
        raise httpx.HTTPStatusError("rejected", request=request, response=response)

    def close(self) -> None:
        pass


def test_revoked_token_stops_the_agent_instead_of_retrying(tmp_path):
    agent = TrackerAgent(_config(tmp_path))
    agent.client = _Rejecting()

    for _ in range(MAX_REJECTIONS):
        agent._send_heartbeat()

    assert agent.revoked is True
    assert agent.stop_event.is_set()
    assert agent.status == "Enrollment token revoked"


def test_transient_rejection_does_not_stop_the_agent(tmp_path):
    agent = TrackerAgent(_config(tmp_path))
    agent.client = _Rejecting()

    agent._send_heartbeat()

    assert agent.revoked is False
    assert not agent.stop_event.is_set()


def test_non_401_rejection_never_marks_the_token_revoked(tmp_path):
    agent = TrackerAgent(_config(tmp_path))
    agent.client = _Rejecting(status_code=500)

    for _ in range(MAX_REJECTIONS + 2):
        agent._send_heartbeat()

    assert agent.revoked is False
    assert not agent.stop_event.is_set()


def test_stop_is_idempotent_so_shutdown_always_closes_the_session(tmp_path):
    """The signal handler sets stop_event, then run()'s finally calls stop()."""
    agent = TrackerAgent(_config(tmp_path))
    sent: list[str] = []
    agent.client = type(
        "Client",
        (),
        {
            "heartbeat": lambda _self, event: sent.append(event.status) or "",
            "close": lambda _self: None,
        },
    )()

    agent.stop_event.set()  # what a SIGTERM handler does
    agent.stop()
    agent.stop()

    assert sent == ["stopped"]


def test_journal_failure_does_not_kill_worker_and_shutdown_still_closes_client(
    tmp_path, monkeypatch
):
    agent = TrackerAgent(_config(tmp_path))
    closed = []
    agent.client = type(
        "Client",
        (),
        {
            "close": lambda _self: closed.append(True),
        },
    )()
    monkeypatch.setattr(
        agent.queue,
        "add_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("disk full")
        ),
    )
    monkeypatch.setattr(
        agent.queue,
        "add_usage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("disk full")
        ),
    )
    agent.start_tracking()
    worker = threading.Thread(target=agent._work_loop, daemon=True)
    agent._worker = worker
    worker.start()
    time.sleep(0.7)

    assert worker.is_alive()
    assert "not saved" in agent.status
    agent.stop()
    assert closed == [True]


@pytest.mark.parametrize("name", ["SIGTERM", "SIGINT"])
def test_shutdown_signal_stops_tracking(tmp_path, name):
    agent = TrackerAgent(_config(tmp_path))
    original = signal.getsignal(getattr(signal, name))
    try:
        installed = install_shutdown_handlers(agent)
        number = int(getattr(signal, name))
        assert number in installed

        signal.getsignal(number)(number, None)
        assert agent.stop_event.is_set()
    finally:
        signal.signal(getattr(signal, name), original)


def test_capture_is_downscaled_by_a_whole_factor():
    image = Image.new("RGB", (3840, 2160), "white")

    reduced = capture.downscale(image, 1920)

    assert max(reduced.size) <= 1920
    assert reduced.size == (1920, 1080)


def test_capture_is_left_alone_when_already_small_enough():
    image = Image.new("RGB", (1280, 720), "white")

    assert capture.downscale(image, 1920) is image
    assert capture.downscale(image, 0) is image


def test_downscaling_shrinks_the_encoded_upload():
    image = Image.new("RGB", (3840, 2160))
    for y in range(0, 2160, 9):
        for x in range(0, 3840, 9):
            image.putpixel((x, y), (x % 256, y % 256, (x + y) % 256))

    full = capture._as_jpeg(image, 65, 0)
    small = capture._as_jpeg(image, 65, 1920)

    assert len(small) < len(full) / 2
    with Image.open(BytesIO(small)) as decoded:
        assert max(decoded.size) <= 1920


def test_max_image_dimension_is_validated(tmp_path):
    with pytest.raises(ValueError, match="max_image_dimension"):
        _config(tmp_path, max_image_dimension=100).validate()

    _config(tmp_path, max_image_dimension=0).validate()
    _config(tmp_path, max_image_dimension=1920).validate()


def test_idle_timeout_is_validated(tmp_path):
    with pytest.raises(ValueError, match="idle_timeout_seconds"):
        _config(tmp_path, idle_timeout_seconds=30).validate()

    _config(tmp_path, idle_timeout_seconds=0).validate()  # disabled
    _config(tmp_path, idle_timeout_seconds=1800).validate()


def test_website_bridge_requires_a_strong_local_token(tmp_path):
    with pytest.raises(ValueError, match="website_bridge_token"):
        _config(tmp_path, website_bridge_token="short").validate()

    _config(tmp_path, website_bridge_token="t" * 40).validate()


class _FakeActivity:
    def __init__(self, idle, available=True):
        self._idle = idle
        self.input_available = available

    def seconds_since_input(self, now=None):
        return self._idle


class _FakeSystemIdle:
    def __init__(self, idle):
        self._idle = idle

    def seconds(self, now=None):
        return self._idle


def test_long_idle_suspends_tracking_and_input_resumes_it(tmp_path):
    agent = TrackerAgent(_config(tmp_path, idle_timeout_seconds=1800))
    agent.start_tracking()
    agent.system_idle = _FakeSystemIdle(None)
    agent.activity = _FakeActivity(idle=2000)

    agent._update_idle_state(now=10_000.0)
    assert agent._auto_idle is True
    assert agent._suspended() is True

    agent.activity = _FakeActivity(idle=1)
    agent._update_idle_state(now=10_050.0)
    assert agent._auto_idle is False
    assert agent._suspended() is False


def test_idle_is_never_assumed_when_input_is_unobservable(tmp_path):
    """On Wayland seconds_since_input is None; absence of a signal is not idleness."""
    agent = TrackerAgent(_config(tmp_path, idle_timeout_seconds=1800))
    agent.start_tracking()
    agent.system_idle = _FakeSystemIdle(None)
    agent.activity = _FakeActivity(idle=None, available=False)

    agent._update_idle_state(now=10_000.0)

    assert agent._auto_idle is False


def test_idle_deduction_can_be_disabled(tmp_path):
    agent = TrackerAgent(_config(tmp_path, idle_timeout_seconds=0))
    agent.start_tracking()
    agent.system_idle = _FakeSystemIdle(None)
    agent.activity = _FakeActivity(idle=999_999)

    agent._update_idle_state(now=10_000.0)

    assert agent._auto_idle is False


def test_standard_timer_opens_stopped_and_requires_a_project(tmp_path):
    agent = TrackerAgent(_config(tmp_path, project_id=""))

    assert agent.timer_state == "stopped"
    assert agent.tracking_active is False
    assert agent.status == "Not tracking"
    with pytest.raises(ValueError, match="Choose a project"):
        agent.start_tracking()


def test_explicit_timer_transitions_are_journalled_in_order(tmp_path):
    agent = TrackerAgent(_config(tmp_path))
    agent.client = type(
        "OfflineClient",
        (),
        {
            "heartbeat": lambda _self, _event: (_ for _ in ()).throw(
                httpx.ConnectError("offline")
            ),
            "close": lambda _self: None,
        },
    )()

    agent.start_tracking()
    agent._send_heartbeat()
    agent.pause_tracking()
    agent._send_heartbeat()
    agent.resume_tracking()
    agent._send_heartbeat()
    agent.stop_tracking()
    agent._send_heartbeat()

    events = agent.queue.pending_states(limit=10)
    assert [event.status for event in events] == [
        "active",
        "paused",
        "active",
        "stopped",
    ]
    assert events[0].project_id == PROJECT_ID
    assert events[-1].project_id == ""


def test_pause_is_invalid_until_timer_has_started(tmp_path):
    agent = TrackerAgent(_config(tmp_path))

    with pytest.raises(ValueError, match="Start the timer"):
        agent.toggle_pause()


def test_current_project_rejection_stops_local_timer(tmp_path):
    agent = TrackerAgent(_config(tmp_path))

    class RejectCurrentWork:
        @staticmethod
        def heartbeat(event):
            request = httpx.Request("POST", "http://server/api/v1/heartbeat")
            response = httpx.Response(
                422,
                request=request,
                json={"detail": "The current timesheet period is approved and locked"},
            )
            raise httpx.HTTPStatusError("locked", request=request, response=response)

        @staticmethod
        def close():
            pass

    agent.client = RejectCurrentWork()
    agent.start_tracking()
    agent._send_heartbeat()

    assert agent.timer_state == "stopped"
    assert agent.tracking_active is False
    assert agent.queue.state_count() == 0
    assert agent.queue.quarantine_count() == 1
    assert agent.status == "Tracking stopped · server rejected this work"


def test_capture_button_does_nothing_while_standard_timer_is_stopped(tmp_path):
    agent = TrackerAgent(_config(tmp_path))

    agent.capture_now()

    assert not agent.capture_event.is_set()


def test_wayland_capture_rotates_encrypted_restore_token(tmp_path, monkeypatch):
    agent = TrackerAgent(_config(tmp_path))
    agent.queue.set_local_state(
        agent_main.WAYLAND_RESTORE_TOKEN_STATE, "previous-token"
    )
    observed = {}

    def screenshot(**options):
        observed.update(options)
        options["save_wayland_restore_token"]("next-token")
        return b"jpeg payload"

    monkeypatch.setattr(agent_main, "is_wayland", lambda: True)
    monkeypatch.setattr(agent_main, "capture_screenshot", screenshot)

    agent._capture_to_queue()

    assert observed["wayland_restore_token"] == "previous-token"
    assert (
        agent.queue.local_state(agent_main.WAYLAND_RESTORE_TOKEN_STATE) == "next-token"
    )
    assert agent.queue.count() == 1
    assert b"next-token" not in agent.queue.database_path.read_bytes()


def _automatic_policy(**overrides):
    policy = {
        "id": "automatic-policy",
        "name": "Engineering hours",
        "consent_status": "accepted",
        "rule_type": "fixed_schedule",
        "project_id": "22222222-2222-4222-8222-222222222222",
        "wait_for_activity": False,
        "schedule": {"mon": [{"start": "09:00", "end": "17:00"}]},
        "shift_windows": [],
    }
    policy.update(overrides)
    return policy


def test_accepted_policy_starts_and_stops_only_its_own_timer(tmp_path):
    agent = TrackerAgent(_config(tmp_path))
    agent._automatic_policy = _automatic_policy()

    agent._apply_automatic_tracking(datetime(2026, 9, 7, 10, 0, tzinfo=UTC), now=100.0)

    assert agent.timer_state == "active"
    assert agent.selected_project_id == "22222222-2222-4222-8222-222222222222"
    assert agent._automatic_started is True
    agent._apply_automatic_tracking(datetime(2026, 9, 7, 17, 0, tzinfo=UTC), now=200.0)
    assert agent.timer_state == "stopped"
    assert agent._automatic_started is False


def test_manual_stop_suppresses_restart_for_same_automatic_window(tmp_path):
    agent = TrackerAgent(_config(tmp_path))
    agent._automatic_policy = _automatic_policy()
    during = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
    agent._apply_automatic_tracking(during, now=100.0)

    agent.stop_tracking()
    agent._apply_automatic_tracking(during + timedelta(minutes=1), now=160.0)

    assert agent.timer_state == "stopped"
    assert agent._automatic_started is False


def test_manual_automatic_stop_survives_application_restart(tmp_path):
    config = _config(tmp_path)
    during = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
    first = TrackerAgent(config)
    first._automatic_policy = _automatic_policy()
    first._apply_automatic_tracking(during, now=100.0)
    first.stop_tracking()

    reopened = TrackerAgent(config)
    reopened._automatic_policy = _automatic_policy()
    reopened._apply_automatic_tracking(during + timedelta(minutes=5), now=400.0)

    assert reopened.timer_state == "stopped"
    assert "suppressed" in reopened.status


def test_wait_for_activity_never_guesses_when_os_signals_are_unavailable(tmp_path):
    agent = TrackerAgent(_config(tmp_path))
    agent._automatic_policy = _automatic_policy(wait_for_activity=True)
    agent.system_idle = _FakeSystemIdle(None)
    agent.activity = _FakeActivity(idle=None, available=False)
    during = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)

    agent._apply_automatic_tracking(during, now=100.0)

    assert agent.timer_state == "stopped"
    assert agent.status == "Automatic schedule waiting for activity"


def test_policy_consent_is_persisted_before_it_becomes_active(tmp_path):
    agent = TrackerAgent(_config(tmp_path))
    agent._automatic_policy = _automatic_policy(consent_status="pending")
    responses = []
    agent.client = type(
        "ConsentClient",
        (),
        {
            "automatic_tracking_consent": lambda _self, policy_id, accepted: (
                responses.append((policy_id, accepted))
            ),
            "close": lambda _self: None,
        },
    )()

    assert agent.pending_automatic_policy["id"] == "automatic-policy"
    agent.respond_automatic_policy("automatic-policy", True)

    assert responses == [("automatic-policy", True)]
    assert agent.pending_automatic_policy is None
    assert agent._automatic_policy["consent_status"] == "accepted"


def test_browser_extension_controls_only_catalogued_work(tmp_path):
    agent = TrackerAgent(_config(tmp_path))
    agent._projects = [
        {
            "id": PROJECT_ID,
            "name": "Assigned project",
            "tasks": [
                {
                    "id": "33333333-3333-4333-8333-333333333333",
                    "name": "Assigned task",
                }
            ],
        }
    ]

    with pytest.raises(ValueError, match="assigned project"):
        agent.browser_timer_action("start", "unknown", "", "")
    with pytest.raises(ValueError, match="selected project"):
        agent.browser_timer_action("start", PROJECT_ID, "unknown", "")
    started = agent.browser_timer_action(
        "start",
        PROJECT_ID,
        "33333333-3333-4333-8333-333333333333",
        "Review pull request",
    )
    assert started["state"] == "active"
    assert started["task_id"] == "33333333-3333-4333-8333-333333333333"
    assert "Review pull request" not in str(started)
    assert agent.browser_timer_action("pause", "", "", "")["state"] == "paused"
    assert agent.browser_timer_action("resume", "", "", "")["state"] == "active"
    assert agent.browser_timer_action("stop", "", "", "")["state"] == "stopped"


def test_project_catalog_is_encrypted_and_available_after_offline_restart(tmp_path):
    config = _config(tmp_path)
    first = TrackerAgent(config)
    first.client = type(
        "CatalogClient",
        (),
        {
            "configuration": lambda _self: {
                "projects": [
                    {
                        "id": PROJECT_ID,
                        "name": "Secret customer project",
                        "tasks": [
                            {
                                "id": "33333333-3333-4333-8333-333333333333",
                                "name": "Private task",
                            }
                        ],
                    }
                ],
                "automatic_tracking": None,
            },
            "close": lambda _self: None,
        },
    )()
    first.catalog()

    local_bytes = b"".join(
        path.read_bytes() for path in config.queue_dir.iterdir() if path.is_file()
    )
    assert b"Secret customer project" not in local_bytes
    assert b"Private task" not in local_bytes
    reopened = TrackerAgent(config)
    snapshot = reopened.browser_timer_snapshot()
    assert snapshot["projects"][0]["name"] == "Secret customer project"
    assert snapshot["projects"][0]["tasks"][0]["name"] == "Private task"

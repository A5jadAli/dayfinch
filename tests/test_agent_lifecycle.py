import signal
from io import BytesIO

import httpx
import pytest
from PIL import Image

from agent import capture
from agent.config import AgentConfig
from agent.main import MAX_REJECTIONS, TrackerAgent, install_shutdown_handlers


def _config(tmp_path, **overrides) -> AgentConfig:
    values = {
        "server_url": "http://127.0.0.1:8000",
        "device_token": "t" * 40,
        "consent_confirmed": True,
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
    agent.system_idle = _FakeSystemIdle(None)
    agent.activity = _FakeActivity(idle=None, available=False)

    agent._update_idle_state(now=10_000.0)

    assert agent._auto_idle is False


def test_idle_deduction_can_be_disabled(tmp_path):
    agent = TrackerAgent(_config(tmp_path, idle_timeout_seconds=0))
    agent.system_idle = _FakeSystemIdle(None)
    agent.activity = _FakeActivity(idle=999_999)

    agent._update_idle_state(now=10_000.0)

    assert agent._auto_idle is False

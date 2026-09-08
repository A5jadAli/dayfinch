from pathlib import Path

import httpx

from agent.activity import ActivitySnapshot
from agent.config import AgentConfig
from agent.main import TrackerAgent


class PolicyClient:
    def __init__(self, policy):
        self.policy = policy

    def configuration(self):
        return self.policy

    @staticmethod
    def upload(_record, _screenshot):
        raise AssertionError("corrupt ciphertext must not be uploaded")


def _agent(tmp_path, policy):
    agent = TrackerAgent(
        AgentConfig(
            server_url="http://127.0.0.1:8000",
            device_token="policy-device-token-that-is-long-enough",
            consent_confirmed=True,
            queue_dir=tmp_path / "queue",
        )
    )
    agent.client = PolicyClient(policy)
    return agent


def test_policy_variant_a_disables_screenshots_and_context(tmp_path):
    agent = _agent(
        tmp_path,
        {
            "screenshot_frequency": 0,
            "screenshot_blur": False,
            "track_apps": False,
            "track_urls": False,
            "idle_timeout_minutes": 30,
        },
    )

    agent.catalog()

    assert agent._capture_interval_seconds == 86_400
    assert agent._screenshots_enabled is False
    assert agent._track_apps is False
    assert agent._track_urls is False
    assert agent._idle_timeout_seconds == 1_800


def test_policy_variant_b_uses_three_blurred_captures_per_ten_minutes(tmp_path):
    agent = _agent(
        tmp_path,
        {
            "screenshot_frequency": 3,
            "screenshot_blur": True,
            "track_apps": True,
            "track_urls": True,
            "idle_timeout_minutes": 10,
        },
    )

    agent.catalog()

    assert agent._capture_interval_seconds == 200
    assert agent._screenshots_enabled is True
    assert agent._screenshot_blur is True
    assert agent._track_apps is True
    assert agent._track_urls is True
    assert agent._idle_timeout_seconds == 600


def test_queued_capture_is_discarded_if_server_now_disables_screenshots(tmp_path):
    agent = _agent(tmp_path, {})
    record = agent.queue.add(b"jpeg", ActivitySnapshot(0, 0, 0), "Editor")

    class DisabledClient:
        @staticmethod
        def upload(_record, _screenshot):
            request = httpx.Request("POST", "http://server/api/v1/activity")
            response = httpx.Response(
                403,
                request=request,
                json={"detail": "Screenshot collection is disabled by policy"},
            )
            raise httpx.HTTPStatusError("disabled", request=request, response=response)

    agent.client = DisabledClient()
    assert agent._upload_one() is False
    assert agent.queue.count() == 0
    assert not Path(record.screenshot_path).exists()
    assert agent._screenshots_enabled is False


def test_corrupt_offline_capture_is_quarantined_without_blocking_queue(tmp_path):
    agent = _agent(tmp_path, {})
    record = agent.queue.add(
        b"sensitive screenshot", ActivitySnapshot(0, 0, 0), "Sensitive App"
    )
    content = bytearray(Path(record.screenshot_path).read_bytes())
    content[-1] ^= 1
    Path(record.screenshot_path).write_bytes(content)

    assert agent._upload_one() is False

    assert agent.queue.count() == 0
    assert agent.queue.quarantine_count() == 1
    metadata = next((tmp_path / "queue" / "quarantine").glob("*.meta.dfq"))
    assert b"Sensitive App" not in metadata.read_bytes()
    assert "quarantined" in agent.status

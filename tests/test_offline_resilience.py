from datetime import UTC, datetime

import httpx

from agent.activity import ActivitySnapshot
from agent.config import AgentConfig
from agent.main import TrackerAgent


class OfflineThenOnline:
    def __init__(self):
        self.online = False
        self.events = []
        self.uploads = []

    def heartbeat(self, event):
        if not self.online:
            raise httpx.ConnectError("wifi unavailable")
        self.events.append(event)
        return "reconstructed-session"

    def upload(self, record, screenshot):
        if not self.online:
            raise httpx.ConnectError("wifi unavailable")
        self.uploads.append((record, screenshot))

    def upload_usage(self, event):
        if not self.online:
            raise httpx.ConnectError("wifi unavailable")
        self.events.append(event)

    def close(self):
        pass


def test_wifi_outage_keeps_time_activity_and_domain_then_syncs_in_order(tmp_path):
    config = AgentConfig(
        server_url="http://127.0.0.1:8000",
        device_token="offline-device-token-that-is-long-enough",
        consent_confirmed=True,
        project_id="11111111-1111-4111-8111-111111111111",
        queue_dir=tmp_path / "queue",
    )
    agent = TrackerAgent(config)
    client = OfflineThenOnline()
    agent.client = client

    agent.start_tracking()
    agent._send_heartbeat()
    agent.queue.add(
        b"\xff\xd8\xffoffline-capture",
        ActivitySnapshot(12, 5, 300, focused_seconds=60, interactive_seconds=25),
        "Google Chrome",
        datetime.now(UTC),
        active_url="docs.example.test",
    )
    agent.queue.add_usage("Google Chrome", "docs.example.test", 10)
    assert agent.queue.state_count() == 1
    assert agent.queue.count() == 1
    assert agent.queue.usage_count() == 1
    assert not agent.stop_event.is_set()

    client.online = True
    assert agent._upload_state_one() is True
    assert agent._session_id == "reconstructed-session"
    assert agent._upload_usage_one() is True
    assert agent._upload_one() is True
    assert agent.queue.state_count() == 0
    assert agent.queue.count() == 0
    assert agent.queue.usage_count() == 0
    assert client.events[0].status == "active"
    assert client.events[1].active_url == "docs.example.test"
    uploaded, screenshot = client.uploads[0]
    assert screenshot == b"\xff\xd8\xffoffline-capture"
    assert uploaded.active_url == "docs.example.test"
    assert uploaded.keyboard_events == 12

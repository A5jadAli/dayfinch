from pathlib import Path

import pytest

from tracker_agent.config import AgentConfig


TOKEN = "x" * 40


def config(**overrides):
    values = {
        "server_url": "https://tracker.example.test",
        "device_token": TOKEN,
        "consent_confirmed": True,
        "queue_dir": Path("queue"),
    }
    values.update(overrides)
    return AgentConfig(**values)


def test_consent_is_required():
    with pytest.raises(ValueError, match="consent"):
        config(consent_confirmed=False).validate()


def test_plain_http_is_local_only():
    with pytest.raises(ValueError, match="HTTPS"):
        config(server_url="http://tracker.example.test").validate()
    config(server_url="http://127.0.0.1:8000").validate()


def test_capture_interval_has_safe_minimum():
    with pytest.raises(ValueError, match="capture_interval"):
        config(capture_interval_seconds=10).validate()

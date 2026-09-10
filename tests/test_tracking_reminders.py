import json
from datetime import UTC, datetime, timedelta

import pytest

from agent.config import AgentConfig
from agent.main import TrackerAgent
from agent.reminders import (
    ReminderSettings,
    active_reminder_window,
    reminder_is_due,
)

PROJECT_ID = "11111111-1111-4111-8111-111111111111"


def _moment(day: int, hour: int, minute: int = 0) -> datetime:
    # 2026-09-07 is a Monday.
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


def _settings(**overrides) -> ReminderSettings:
    values = {
        "enabled": True,
        "start": "09:00",
        "end": "17:00",
        "days": ["mon", "tue", "wed", "thu", "fri"],
        "interval_minutes": 15,
        "mode": "notification",
    }
    values.update(overrides)
    return ReminderSettings.from_mapping(values, strict=True)


def _agent(tmp_path) -> TrackerAgent:
    return TrackerAgent(
        AgentConfig(
            server_url="http://127.0.0.1:8000",
            device_token="t" * 40,
            consent_confirmed=True,
            project_id=PROJECT_ID,
            queue_dir=tmp_path / "queue",
        )
    )


def test_reminder_settings_reject_unsafe_or_ambiguous_values():
    with pytest.raises(ValueError, match="valid start/end"):
        _settings(start="9am")
    with pytest.raises(ValueError, match="valid start/end"):
        _settings(start="09:00", end="09:00")
    with pytest.raises(ValueError, match="valid start/end"):
        _settings(days=[])
    with pytest.raises(ValueError, match="valid start/end"):
        _settings(days=["mon", "mon"])
    with pytest.raises(ValueError, match="5–240"):
        _settings(interval_minutes=4)


def test_reminder_window_uses_selected_local_days_and_excludes_end():
    settings = _settings()

    window = active_reminder_window(settings, _moment(7, 9))

    assert window
    assert window.starts_at == _moment(7, 9)
    assert window.ends_at == _moment(7, 17)
    assert active_reminder_window(settings, _moment(7, 16, 59))
    assert active_reminder_window(settings, _moment(7, 17)) is None
    assert active_reminder_window(settings, _moment(12, 12)) is None  # Saturday


def test_overnight_reminder_window_belongs_to_its_start_day():
    settings = _settings(start="22:00", end="06:00", days=["mon"])

    monday = active_reminder_window(settings, _moment(7, 23))
    tuesday = active_reminder_window(settings, _moment(8, 2))

    assert monday and tuesday
    assert monday.key == tuesday.key
    assert tuesday.ends_at == _moment(8, 6)
    assert active_reminder_window(settings, _moment(8, 22)) is None


def test_reminders_repeat_at_interval_and_clock_rollback_does_not_silence_them():
    settings = _settings(interval_minutes=15)
    first = _moment(7, 9)

    assert reminder_is_due(settings, first, None)
    assert reminder_is_due(settings, first + timedelta(minutes=14), first) is None
    assert reminder_is_due(settings, first + timedelta(minutes=15), first)
    assert reminder_is_due(settings, first, first + timedelta(hours=1))


def test_agent_reminders_are_encrypted_restart_safe_and_stop_while_tracking(tmp_path):
    agent = _agent(tmp_path)
    saved = agent.configure_tracking_reminders(_settings(interval_minutes=5).as_dict())
    first = _moment(7, 9)

    assert saved["enabled"] is True
    assert agent.tracking_reminder(first)["mode"] == "notification"
    assert agent.tracking_reminder(first + timedelta(minutes=4)) is None

    agent.start_tracking()
    assert agent.tracking_reminder(first + timedelta(minutes=5)) is None
    agent.pause_tracking()
    assert agent.tracking_reminder(first + timedelta(minutes=10)) is None

    restarted = _agent(tmp_path)
    assert restarted.tracking_reminder_settings == saved
    assert restarted.tracking_reminder(first + timedelta(minutes=4)) is None
    assert restarted.tracking_reminder(first + timedelta(minutes=5))

    raw = b"".join(
        path.read_bytes() for path in (tmp_path / "queue").rglob("*") if path.is_file()
    )
    assert b'"start":"09:00"' not in raw
    assert b"2026-09-07T09:05:00" not in raw


def test_stopping_during_a_window_defers_the_next_reminder(tmp_path):
    agent = _agent(tmp_path)
    agent.configure_tracking_reminders(_settings(interval_minutes=15).as_dict())
    moment = _moment(7, 10)
    agent.defer_tracking_reminder(moment)

    assert agent.tracking_reminder(moment + timedelta(minutes=14)) is None
    assert agent.tracking_reminder(moment + timedelta(minutes=15))


def test_invalid_saved_settings_fail_closed_to_disabled(tmp_path):
    agent = _agent(tmp_path)
    agent.queue.set_local_state(
        "tracking_reminders",
        '{"settings":{"enabled":true,"start":"invalid"},"last_sent_at":""}',
    )

    reopened = _agent(tmp_path)

    assert reopened.tracking_reminder_settings["enabled"] is False
    assert reopened.tracking_reminder(_moment(7, 10)) is None


def test_legacy_reminder_state_is_read_during_upgrade(tmp_path):
    agent = _agent(tmp_path)
    settings = _settings(mode="alert")
    agent.queue.set_local_state(
        "tracking_reminder_settings",
        json.dumps(settings.as_dict()),
    )
    agent.queue.set_local_state(
        "tracking_reminder_last_sent", _moment(7, 9).isoformat()
    )

    reopened = _agent(tmp_path)

    assert reopened.tracking_reminder_settings["mode"] == "alert"
    assert reopened.tracking_reminder(_moment(7, 9, 14)) is None
    assert reopened.tracking_reminder(_moment(7, 9, 15))["mode"] == "alert"


def test_failed_reminder_save_does_not_publish_a_transient_preference(
    tmp_path, monkeypatch
):
    agent = _agent(tmp_path)
    before = agent.tracking_reminder_settings
    monkeypatch.setattr(
        agent.queue,
        "set_local_state",
        lambda *_args: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        agent.configure_tracking_reminders(_settings().as_dict())

    assert agent.tracking_reminder_settings == before

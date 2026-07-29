"""Idle exposure and synthetic-input detection in the activity monitor."""

from __future__ import annotations

from agent.activity import ActivityMonitor


def _monitor() -> ActivityMonitor:
    monitor = ActivityMonitor()
    # Simulate listeners having attached without touching real input devices.
    monitor._input_available = True
    return monitor


def test_seconds_since_input_is_none_when_input_unavailable():
    monitor = ActivityMonitor()  # input_available stays False (e.g. Wayland)
    monitor._on_press(None)

    assert monitor.seconds_since_input() is None


def test_seconds_since_input_grows_from_last_event():
    monitor = _monitor()
    monitor._last_input_at = 100.0

    assert monitor.seconds_since_input(now=130.0) == 30.0


def test_metronomic_events_are_flagged_as_automation():
    monitor = _monitor()
    now = 0.0
    for _ in range(60):  # perfectly regular 0.5s cadence: a key-repeat macro
        now += 0.5
        monitor._last_event_at = now - 0.5
        monitor._last_input_at = now
        monitor._event_gaps.append(0.5)
        monitor._keyboard_events += 1

    snapshot = monitor.snapshot_and_reset()

    assert snapshot.automation_suspected is True
    assert snapshot.interactive_seconds == 0


def test_oscillating_mouse_delta_is_flagged_as_a_jiggler():
    monitor = _monitor()
    for _ in range(40):
        monitor._move_deltas.extend(((3, 0), (-3, 0)))

    snapshot = monitor.snapshot_and_reset()

    assert snapshot.automation_suspected is True


def test_human_like_variation_is_not_flagged():
    monitor = _monitor()
    gaps = [0.12, 0.4, 0.05, 0.9, 0.3, 0.6, 0.18, 1.4, 0.22, 0.7] * 4
    for gap in gaps:
        monitor._event_gaps.append(gap)
        monitor._keyboard_events += 1
    for i in range(40):
        monitor._move_deltas.append((i % 7 - 3, i % 5 - 2))
    monitor._interactive_seconds = 42.0

    snapshot = monitor.snapshot_and_reset()

    assert snapshot.automation_suspected is False
    assert snapshot.interactive_seconds == 42


def test_snapshot_resets_detection_state():
    monitor = _monitor()
    for _ in range(40):
        monitor._move_deltas.extend(((3, 0), (-3, 0)))

    assert monitor.snapshot_and_reset().automation_suspected is True
    # A fresh interval with no input must not inherit the previous verdict.
    assert monitor.snapshot_and_reset().automation_suspected is False


def test_straight_human_mouse_movement_is_not_treated_as_proof():
    monitor = _monitor()
    for _ in range(60):
        monitor._move_deltas.append((3, 0))

    assert monitor.snapshot_and_reset().automation_suspected is False

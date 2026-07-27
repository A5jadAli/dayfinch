from agent.activity import ActivityMonitor


def test_activity_monitor_keeps_counts_not_key_values():
    monitor = ActivityMonitor()
    monitor._on_press("a")
    monitor._on_press("secret-value")
    monitor._on_click(1, 1, object(), True)
    monitor._on_move(0, 0)
    monitor._on_move(3, 4)

    snapshot = monitor.snapshot_and_reset()

    assert snapshot.keyboard_events == 2
    assert snapshot.mouse_clicks == 1
    assert snapshot.mouse_distance == 5
    assert not hasattr(snapshot, "keys")
    assert monitor.snapshot_and_reset().keyboard_events == 0


def test_pause_discards_activity():
    monitor = ActivityMonitor()
    monitor.set_enabled(False)
    monitor._on_press("a")
    monitor._on_click(1, 1, object(), True)
    assert monitor.snapshot_and_reset().keyboard_events == 0
    assert monitor.snapshot_and_reset().mouse_clicks == 0


def test_foreground_observation_counts_reading_without_faking_input():
    monitor = ActivityMonitor(max_observation_gap=15)
    monitor.observe("Editor", now=10)
    monitor.observe("Editor", now=15)
    monitor.observe("Editor", now=20)

    snapshot = monitor.snapshot_and_reset()

    assert snapshot.focused_seconds == 10
    assert snapshot.interactive_seconds == 0
    assert snapshot.keyboard_events == 0


def test_unknown_apps_and_paused_gaps_are_not_counted():
    monitor = ActivityMonitor(max_observation_gap=15)
    monitor.observe("Unknown", now=10)
    monitor.observe("Unknown", now=15)
    monitor.set_enabled(False)
    monitor.observe("Editor", now=100)
    monitor.set_enabled(True)
    monitor.observe("Editor", now=200)
    monitor.observe("Editor", now=205)

    assert monitor.snapshot_and_reset().focused_seconds == 5

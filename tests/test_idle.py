from __future__ import annotations

import subprocess

from agent import idle


def test_gnome_idle_monitor_milliseconds_are_converted(monkeypatch):
    monkeypatch.setattr(
        idle.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, "(uint64 12500,)\n", ""
        ),
    )

    assert idle._linux_idle_seconds() == 12.5


def test_system_idle_monitor_caches_desktop_query(monkeypatch):
    calls = []
    monkeypatch.setattr(
        idle, "_system_idle_seconds", lambda: calls.append(True) or 42.0
    )
    monitor = idle.SystemIdleMonitor(poll_interval_seconds=5)

    assert monitor.seconds(now=10) == 42.0
    assert monitor.seconds(now=12) == 42.0
    assert len(calls) == 1

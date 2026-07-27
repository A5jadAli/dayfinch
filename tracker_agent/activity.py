from __future__ import annotations

import math
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class ActivitySnapshot:
    keyboard_events: int
    mouse_clicks: int
    mouse_distance: int


class ActivityMonitor:
    """Counts events only. It never stores the value of a pressed key."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._enabled = True
        self._keyboard_events = 0
        self._mouse_clicks = 0
        self._mouse_distance = 0.0
        self._last_position: tuple[int, int] | None = None
        self._keyboard_listener = None
        self._mouse_listener = None

    def start(self) -> None:
        from pynput import keyboard, mouse

        # Callback arguments are intentionally ignored. Only aggregate counts exist.
        self._keyboard_listener = keyboard.Listener(on_press=self._on_press)
        self._mouse_listener = mouse.Listener(
            on_move=self._on_move,
            on_click=self._on_click,
        )
        self._keyboard_listener.start()
        self._mouse_listener.start()

    def stop(self) -> None:
        if self._keyboard_listener:
            self._keyboard_listener.stop()
        if self._mouse_listener:
            self._mouse_listener.stop()

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = enabled
            self._last_position = None
            if not enabled:
                self._reset_unlocked()

    def snapshot_and_reset(self) -> ActivitySnapshot:
        with self._lock:
            snapshot = ActivitySnapshot(
                keyboard_events=self._keyboard_events,
                mouse_clicks=self._mouse_clicks,
                mouse_distance=round(self._mouse_distance),
            )
            self._reset_unlocked()
            return snapshot

    def _reset_unlocked(self) -> None:
        self._keyboard_events = 0
        self._mouse_clicks = 0
        self._mouse_distance = 0.0

    def _on_press(self, _key: object) -> None:
        with self._lock:
            if self._enabled:
                self._keyboard_events += 1

    def _on_click(self, _x: int, _y: int, _button: object, pressed: bool) -> None:
        if not pressed:
            return
        with self._lock:
            if self._enabled:
                self._mouse_clicks += 1

    def _on_move(self, x: int, y: int) -> None:
        with self._lock:
            if not self._enabled:
                return
            if self._last_position is not None:
                distance = math.hypot(x - self._last_position[0], y - self._last_position[1])
                # Ignore display jumps and cap noise from unusual drivers.
                if distance < 10_000:
                    self._mouse_distance += distance
            self._last_position = (x, y)

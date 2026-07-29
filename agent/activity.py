from __future__ import annotations

import math
import os
import platform
import statistics
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass

# A synthetic input source (mouse jiggler, auto-clicker, key-repeat macro) produces
# events at a near-constant cadence. Genuine typing and pointing vary a lot, so a
# very low spread across enough events is a strong automation signal. These bias
# toward precision: they only fire on input that is overwhelmingly regular, so real
# users mixing typing and pausing are not flagged.
_MIN_EVENTS_FOR_REGULARITY = 30
_REGULARITY_CV_THRESHOLD = 0.08
_MIN_MOVES_FOR_REPETITION = 40
_OSCILLATING_DELTA_SHARE = 0.9


@dataclass(frozen=True)
class ActivitySnapshot:
    keyboard_events: int
    mouse_clicks: int
    mouse_distance: int
    focused_seconds: int = 0
    interactive_seconds: int = 0
    automation_suspected: bool = False


class ActivityMonitor:
    """Counts events only. It never stores the value of a pressed key."""

    def __init__(
        self, *, idle_grace_seconds: float = 120.0, max_observation_gap: float = 15.0
    ) -> None:
        self._lock = threading.Lock()
        self._enabled = True
        self._keyboard_events = 0
        self._mouse_clicks = 0
        self._mouse_distance = 0.0
        self._last_position: tuple[int, int] | None = None
        self._last_input_at: float | None = None
        self._last_observation_at: float | None = None
        self._focused_seconds = 0.0
        self._interactive_seconds = 0.0
        self._idle_grace_seconds = idle_grace_seconds
        self._max_observation_gap = max_observation_gap
        self._keyboard_listener = None
        self._mouse_listener = None
        self._input_available = False
        # Bounded histories for automation detection; capped so memory stays flat.
        self._event_gaps: deque[float] = deque(maxlen=256)
        self._move_deltas: deque[tuple[int, int]] = deque(maxlen=128)
        self._last_event_at: float | None = None

    @property
    def input_available(self) -> bool:
        """True only when passive input listeners are actually running.

        On Wayland (and anywhere pynput cannot attach) this stays False, so callers
        must never treat "no input" as idle there — absence of a signal is not idle.
        """
        return self._input_available

    def seconds_since_input(self, now: float | None = None) -> float | None:
        moment = time.monotonic() if now is None else now
        with self._lock:
            if not self._input_available or self._last_input_at is None:
                return None
            return max(0.0, moment - self._last_input_at)

    def start(self) -> bool:
        if (
            platform.system() == "Linux"
            and os.getenv("XDG_SESSION_TYPE", "").strip().lower() == "wayland"
        ):
            return False
        try:
            from pynput import keyboard, mouse
        except Exception:
            return False

        # Callback arguments are intentionally ignored. Only aggregate counts exist.
        self._keyboard_listener = keyboard.Listener(on_press=self._on_press)
        self._mouse_listener = mouse.Listener(
            on_move=self._on_move,
            on_click=self._on_click,
        )
        try:
            self._keyboard_listener.start()
            self._mouse_listener.start()
        except Exception:
            self.stop()
            self._keyboard_listener = None
            self._mouse_listener = None
            return False
        self._input_available = True
        return True

    def stop(self) -> None:
        if self._keyboard_listener:
            self._keyboard_listener.stop()
        if self._mouse_listener:
            self._mouse_listener.stop()

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = enabled
            self._last_position = None
            self._last_observation_at = None
            self._last_input_at = None
            self._last_event_at = None
            if not enabled:
                self._reset_unlocked()

    def snapshot_and_reset(self) -> ActivitySnapshot:
        with self._lock:
            automation = self._automation_suspected_unlocked()
            snapshot = ActivitySnapshot(
                keyboard_events=self._keyboard_events,
                mouse_clicks=self._mouse_clicks,
                mouse_distance=round(self._mouse_distance),
                focused_seconds=round(self._focused_seconds),
                # Faked input must not inflate genuine interaction time.
                interactive_seconds=0
                if automation
                else round(self._interactive_seconds),
                automation_suspected=automation,
            )
            self._reset_unlocked()
            return snapshot

    def _automation_suspected_unlocked(self) -> bool:
        gaps = list(self._event_gaps)
        if len(gaps) >= _MIN_EVENTS_FOR_REGULARITY:
            mean = statistics.fmean(gaps)
            if mean > 0:
                spread = statistics.pstdev(gaps) / mean
                if spread < _REGULARITY_CV_THRESHOLD:
                    return True
        moves = list(self._move_deltas)
        if len(moves) >= _MIN_MOVES_FOR_REPETITION:
            counts = Counter(moves)
            for (dx, dy), count in counts.items():
                opposite = counts.get((-dx, -dy), 0)
                if (
                    (dx, dy) != (0, 0)
                    and min(count, opposite) >= len(moves) * 0.2
                    and (count + opposite) / len(moves) >= _OSCILLATING_DELTA_SHARE
                ):
                    return True
        return False

    def _reset_unlocked(self) -> None:
        self._keyboard_events = 0
        self._mouse_clicks = 0
        self._mouse_distance = 0.0
        self._focused_seconds = 0.0
        self._interactive_seconds = 0.0
        self._event_gaps.clear()
        self._move_deltas.clear()
        self._last_event_at = None

    def observe(self, application: str, *, now: float | None = None) -> None:
        """Record foreground-app presence without inspecting titles or content.

        Focus time is intentionally distinct from interaction time: reading code,
        reviewing AI output, and waiting for a build are real work, but should not
        be misrepresented as keyboard activity.
        """
        observed_at = time.monotonic() if now is None else now
        with self._lock:
            previous = self._last_observation_at
            self._last_observation_at = observed_at
            if not self._enabled or previous is None:
                return
            elapsed = min(max(observed_at - previous, 0.0), self._max_observation_gap)
            normalized = application.strip().lower()
            if not normalized or normalized == "unknown":
                return
            self._focused_seconds += elapsed
            if (
                self._last_input_at is not None
                and observed_at - self._last_input_at <= self._idle_grace_seconds
            ):
                self._interactive_seconds += elapsed

    def _mark_input_unlocked(self, *, track_cadence: bool = True) -> None:
        now = time.monotonic()
        if track_cadence and self._last_event_at is not None:
            self._event_gaps.append(now - self._last_event_at)
        if track_cadence:
            self._last_event_at = now
        self._last_input_at = now

    def _on_press(self, _key: object) -> None:
        with self._lock:
            if self._enabled:
                self._keyboard_events += 1
                self._mark_input_unlocked()

    def _on_click(self, _x: int, _y: int, _button: object, pressed: bool) -> None:
        if not pressed:
            return
        with self._lock:
            if self._enabled:
                self._mouse_clicks += 1
                self._mark_input_unlocked()

    def _on_move(self, x: int, y: int) -> None:
        with self._lock:
            if not self._enabled:
                return
            # Pointer drivers emit naturally regular samples while a person moves
            # the mouse, so movement cadence alone is not evidence of automation.
            self._mark_input_unlocked(track_cadence=False)
            if self._last_position is not None:
                dx = x - self._last_position[0]
                dy = y - self._last_position[1]
                self._move_deltas.append((dx, dy))
                distance = math.hypot(dx, dy)
                # Ignore display jumps and cap noise from unusual drivers.
                if distance < 10_000:
                    self._mouse_distance += distance
            self._last_position = (x, y)

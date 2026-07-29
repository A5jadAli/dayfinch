from __future__ import annotations

import ctypes
import platform
import re
import subprocess
import time


class SystemIdleMonitor:
    """Read OS session idleness without recording keys, clicks, or their content."""

    def __init__(self, poll_interval_seconds: float = 5.0) -> None:
        self.poll_interval_seconds = poll_interval_seconds
        self._last_polled = 0.0
        self._cached: float | None = None

    def seconds(self, now: float | None = None) -> float | None:
        moment = time.monotonic() if now is None else now
        if moment - self._last_polled < self.poll_interval_seconds:
            return self._cached
        self._last_polled = moment
        self._cached = _system_idle_seconds()
        return self._cached


def _system_idle_seconds() -> float | None:
    system = platform.system()
    try:
        if system == "Windows":
            return _windows_idle_seconds()
        if system == "Darwin":
            return _macos_idle_seconds()
        if system == "Linux":
            return _linux_idle_seconds()
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None


def _windows_idle_seconds() -> float:
    class LastInputInfo(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LastInputInfo()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        raise OSError("GetLastInputInfo failed")
    # GetTickCount wraps about every 49 days; unsigned subtraction handles it.
    elapsed_ms = ctypes.c_uint(
        ctypes.windll.kernel32.GetTickCount() - info.dwTime
    ).value
    return elapsed_ms / 1000.0


def _macos_idle_seconds() -> float:
    framework = ctypes.CDLL(
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
    )
    function = framework.CGEventSourceSecondsSinceLastEventType
    function.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    function.restype = ctypes.c_double
    # Combined session state, any input event.
    value = float(function(0, 0xFFFFFFFF))
    if value < 0:
        raise OSError("CoreGraphics idle time unavailable")
    return value


def _linux_idle_seconds() -> float:
    """Use desktop idle services that remain available under Wayland.

    GNOME exposes milliseconds through Mutter. KDE and several other desktops
    implement the freedesktop ScreenSaver seconds API. Neither exposes input data.
    """
    calls = (
        (
            "org.gnome.Mutter.IdleMonitor",
            "/org/gnome/Mutter/IdleMonitor/Core",
            "org.gnome.Mutter.IdleMonitor.GetIdletime",
            0.001,
        ),
        (
            "org.freedesktop.ScreenSaver",
            "/org/freedesktop/ScreenSaver",
            "org.freedesktop.ScreenSaver.GetSessionIdleTime",
            1.0,
        ),
    )
    for destination, object_path, method, scale in calls:
        try:
            result = subprocess.run(
                [
                    "gdbus",
                    "call",
                    "--session",
                    "--dest",
                    destination,
                    "--object-path",
                    object_path,
                    "--method",
                    method,
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except FileNotFoundError:
            return None
        if result.returncode != 0:
            continue
        match = re.search(r"(?:uint(?:32|64)\s+)?(\d+)(?=\s*[,)]|$)", result.stdout)
        if match:
            return int(match.group(1)) * scale
    return None

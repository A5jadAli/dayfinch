from __future__ import annotations

import ctypes
import os
import platform
import subprocess

import psutil


def active_application() -> str:
    """Return only the foreground process/app name, never the window title."""
    try:
        system = platform.system()
        if system == "Windows":
            return _windows_app()
        if system == "Darwin":
            return _macos_app()
        if system == "Linux":
            return _linux_app()
    except (OSError, psutil.Error, subprocess.SubprocessError, ValueError):
        pass
    return "Unknown"


def _windows_app() -> str:
    user32 = ctypes.windll.user32
    window = user32.GetForegroundWindow()
    process_id = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(window, ctypes.byref(process_id))
    return psutil.Process(process_id.value).name()[:160]


def _macos_app() -> str:
    result = subprocess.run(
        [
            "/usr/bin/osascript",
            "-e",
            'tell application "System Events" to get name of first application process whose frontmost is true',
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=2,
    )
    return result.stdout.strip()[:160] or "Unknown"


def _linux_app() -> str:
    if os.getenv("XDG_SESSION_TYPE", "").strip().lower() == "wayland":
        desktop = os.getenv("XDG_CURRENT_DESKTOP", "Wayland").split(":", 1)[0]
        return f"{desktop or 'Wayland'} desktop"[:160]
    result = subprocess.run(
        ["xdotool", "getactivewindow", "getwindowpid"],
        check=True,
        capture_output=True,
        text=True,
        timeout=2,
    )
    return psutil.Process(int(result.stdout.strip())).name()[:160]

from __future__ import annotations

import ctypes
import os
import platform
import subprocess
from urllib.parse import urlparse

import psutil

# Browsers whose foreground tab URL we try to read. Matched against the process
# name, case-insensitively.
_BROWSER_PROCESS_HINTS = (
    "chrome",
    "chromium",
    "brave",
    "msedge",
    "edge",
    "safari",
    "arc",
    "vivaldi",
    "opera",
    "firefox",
)

# AppleScript per browser family. Chromium-based browsers share the "active tab of
# front window" model; Safari uses "current tab".
_MACOS_URL_SCRIPTS = {
    "Google Chrome": 'tell application "Google Chrome" to get URL of active tab of front window',
    "Google Chrome Canary": 'tell application "Google Chrome Canary" to get URL of active tab of front window',
    "Brave Browser": 'tell application "Brave Browser" to get URL of active tab of front window',
    "Microsoft Edge": 'tell application "Microsoft Edge" to get URL of active tab of front window',
    "Vivaldi": 'tell application "Vivaldi" to get URL of active tab of front window',
    "Arc": 'tell application "Arc" to get URL of active tab of front window',
    "Safari": 'tell application "Safari" to get URL of current tab of front window',
}


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


def active_website(application: str) -> str:
    """Best-effort domain of the foreground browser tab, host only — never the path.

    Only the site (e.g. ``github.com``) is returned, never the full URL, query, or
    page title, keeping collection proportionate to "which sites were used". Returns
    an empty string when the foreground app is not a known browser or the desktop
    does not allow reading it (Wayland, missing permission, non-browser app).
    """
    if not is_browser_application(application):
        return ""
    try:
        system = platform.system()
        if system == "Darwin":
            return _host(_macos_browser_url(application))
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    # Windows and Linux need a browser extension or accessibility scraping to read
    # the URL reliably; we intentionally report nothing rather than a guess.
    return ""


def is_browser_application(application: str) -> bool:
    lowered = application.lower()
    return any(hint in lowered for hint in _BROWSER_PROCESS_HINTS)


def _host(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    return (parsed.hostname or "").removeprefix("www.")[:255]


def _macos_browser_url(application: str) -> str:
    for app_name, script in _MACOS_URL_SCRIPTS.items():
        if app_name.lower() not in application.lower():
            continue
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return ""
    return ""


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

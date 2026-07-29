from __future__ import annotations

import asyncio
import ctypes
import importlib.util
import os
import platform
import shutil
from dataclasses import dataclass
from typing import Literal

from .idle import _linux_idle_seconds

Status = Literal["pass", "warn", "fail"]


@dataclass(frozen=True)
class DiagnosticCheck:
    name: str
    status: Status
    message: str


def run_diagnostics() -> list[DiagnosticCheck]:
    system = platform.system()
    checks = [
        DiagnosticCheck("operating-system", "pass", f"{system} {platform.release()}"),
        _dependency_check("mss", "screen-capture-library"),
        _dependency_check("pynput", "aggregate-input-library"),
        _dependency_check("pystray", "tray-library"),
    ]
    if system == "Windows":
        checks.extend(_windows_checks())
    elif system == "Darwin":
        checks.extend(_macos_checks())
    elif system == "Linux":
        checks.extend(_linux_checks())
    else:
        checks.append(
            DiagnosticCheck(
                "platform-support",
                "fail",
                f"{system or 'Unknown OS'} is not currently supported",
            )
        )
    return checks


def has_failures(checks: list[DiagnosticCheck]) -> bool:
    return any(check.status == "fail" for check in checks)


def format_diagnostics(checks: list[DiagnosticCheck]) -> str:
    return "\n".join(
        f"[{check.status.upper():4}] {check.name}: {check.message}" for check in checks
    )


def _dependency_check(module: str, name: str) -> DiagnosticCheck:
    available = importlib.util.find_spec(module) is not None
    return DiagnosticCheck(
        name,
        "pass" if available else "fail",
        "available" if available else 'missing; install with pip install -e ".[agent]"',
    )


def _windows_checks() -> list[DiagnosticCheck]:
    interactive = bool(os.getenv("SESSIONNAME"))
    return [
        DiagnosticCheck(
            "desktop-session",
            "pass" if interactive else "warn",
            "interactive Windows session detected"
            if interactive
            else "session could not be confirmed; do not run the agent as a service",
        ),
        DiagnosticCheck(
            "permissions",
            "pass",
            "Windows prompts for any required capture permissions at runtime",
        ),
    ]


def _macos_checks() -> list[DiagnosticCheck]:
    return [
        _macos_permission(
            "screen-recording",
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics",
            "CGPreflightScreenCaptureAccess",
            "fail",
            "enable Screen Recording for Dayfinch in Privacy & Security",
        ),
        _macos_permission(
            "input-monitoring",
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics",
            "CGPreflightListenEventAccess",
            "warn",
            "enable Input Monitoring to collect aggregate activity counts",
        ),
        _macos_permission(
            "accessibility",
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices",
            "AXIsProcessTrusted",
            "warn",
            "enable Accessibility to detect the foreground application",
        ),
    ]


def _macos_permission(
    name: str,
    framework: str,
    function_name: str,
    denied_status: Status,
    denied_message: str,
) -> DiagnosticCheck:
    try:
        library = ctypes.CDLL(framework)
        function = getattr(library, function_name)
        function.restype = ctypes.c_bool
        granted = bool(function())
    except (AttributeError, OSError):
        return DiagnosticCheck(name, "warn", "permission state could not be queried")
    return DiagnosticCheck(
        name,
        "pass" if granted else denied_status,
        "granted" if granted else denied_message,
    )


def _linux_checks() -> list[DiagnosticCheck]:
    session = os.getenv("XDG_SESSION_TYPE", "").strip().lower()
    if session == "wayland":
        portal_dependency = _dependency_check("dbus_next", "wayland-portal-library")
        portal_available = (
            _portal_available() if portal_dependency.status == "pass" else False
        )
        idle_available = _linux_idle_seconds() is not None
        return [
            DiagnosticCheck("display-session", "pass", "Wayland detected"),
            portal_dependency,
            DiagnosticCheck(
                "screenshot-portal",
                "pass" if portal_available else "fail",
                "XDG desktop portal is available"
                if portal_available
                else "start xdg-desktop-portal for consent-aware screenshots",
            ),
            DiagnosticCheck(
                "aggregate-input",
                "warn",
                "Wayland blocks passive global input monitoring; time and focus remain available",
            ),
            DiagnosticCheck(
                "session-idle",
                "pass" if idle_available else "warn",
                "desktop idle time is available for 30-minute deduction"
                if idle_available
                else "desktop idle API unavailable; automatic idle deduction is disabled",
            ),
            DiagnosticCheck(
                "capture-consent-persistence",
                "warn",
                "Screenshot portal may prompt per capture; persistent ScreenCast is not implemented",
            ),
            DiagnosticCheck(
                "foreground-application",
                "warn",
                "Wayland does not expose other applications; the desktop session is reported",
            ),
        ]

    display = bool(os.getenv("DISPLAY"))
    xdotool = shutil.which("xdotool") is not None
    return [
        DiagnosticCheck(
            "display-session",
            "pass" if display else "fail",
            "X11 display detected" if display else "DISPLAY is missing",
        ),
        DiagnosticCheck(
            "foreground-application",
            "pass" if xdotool else "warn",
            "xdotool is available"
            if xdotool
            else "install xdotool to identify the foreground process",
        ),
    ]


def _portal_available() -> bool:
    if not os.getenv("DBUS_SESSION_BUS_ADDRESS"):
        return False
    try:
        return asyncio.run(_portal_name_has_owner())
    except Exception:
        return False


async def _portal_name_has_owner() -> bool:
    from dbus_next import BusType, Message, MessageType
    from dbus_next.aio import MessageBus

    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    try:
        reply = await asyncio.wait_for(
            bus.call(
                Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="NameHasOwner",
                    signature="s",
                    body=["org.freedesktop.portal.Desktop"],
                )
            ),
            timeout=5,
        )
        return reply.message_type != MessageType.ERROR and bool(reply.body[0])
    finally:
        bus.disconnect()

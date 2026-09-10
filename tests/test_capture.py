import asyncio
import os
import sys
from io import BytesIO
from types import SimpleNamespace

from PIL import Image

from agent import capture


def test_capture_uses_portal_on_wayland(monkeypatch):
    monkeypatch.setattr(capture, "is_wayland", lambda: True)
    monkeypatch.setattr(
        capture, "_capture_wayland", lambda quality, max_dimension: b"portal"
    )
    monkeypatch.setattr(
        capture,
        "_capture_mss",
        lambda _all, _quality, _max: (_ for _ in ()).throw(AssertionError()),
    )

    assert capture.capture_screenshot(all_monitors=True, jpeg_quality=65) == b"portal"


def test_capture_uses_mss_outside_wayland(monkeypatch):
    monkeypatch.setattr(capture, "is_wayland", lambda: False)
    monkeypatch.setattr(
        capture, "_capture_mss", lambda all_monitors, quality, max_dimension: b"desktop"
    )

    assert capture.capture_screenshot(all_monitors=False, jpeg_quality=70) == b"desktop"


def test_jpeg_encoding_is_valid():
    data = capture._as_jpeg(Image.new("RGB", (4, 4), "red"), 65)

    with Image.open(BytesIO(data)) as image:
        assert image.format == "JPEG"
        assert image.size == (4, 4)


def test_wayland_portal_response_returns_accessible_file(tmp_path, monkeypatch):
    screenshot = tmp_path / "portal.png"
    Image.new("RGB", (2, 2), "blue").save(screenshot)

    class MessageType:
        SIGNAL = "signal"
        ERROR = "error"
        METHOD_RETURN = "return"

    class Message:
        def __init__(self, **values):
            self.__dict__.update(values)
            self.message_type = MessageType.METHOD_RETURN
            self.body = values.get("body", [])

    class Variant:
        def __init__(self, _signature, value):
            self.value = value

    class FakeBus:
        unique_name = ":1.42"

        def __init__(self):
            self.handlers = []

        async def connect(self):
            return self

        def add_message_handler(self, handler):
            self.handlers.append(handler)

        async def call(self, message):
            if message.member == "Screenshot":
                response = SimpleNamespace(
                    message_type=MessageType.SIGNAL,
                    path=(
                        "/org/freedesktop/portal/desktop/request/1_42/"
                        + message.body[1]["handle_token"].value
                    ),
                    interface="org.freedesktop.portal.Request",
                    member="Response",
                    body=[0, {"uri": Variant("s", screenshot.as_uri())}],
                )
                for handler in self.handlers:
                    handler(response)
            return SimpleNamespace(message_type=MessageType.METHOD_RETURN, body=[])

        def disconnect(self):
            pass

    fake_bus = FakeBus()
    monkeypatch.setitem(
        sys.modules,
        "dbus_next",
        SimpleNamespace(
            BusType=SimpleNamespace(SESSION="session"),
            Message=Message,
            MessageType=MessageType,
            Variant=Variant,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "dbus_next.aio",
        SimpleNamespace(MessageBus=lambda **_kwargs: fake_bus),
    )

    assert asyncio.run(capture._request_portal_screenshot()) == screenshot


def test_persistent_screencast_rotates_token_before_reading_frame(monkeypatch):
    class MessageType:
        SIGNAL = "signal"
        ERROR = "error"
        METHOD_RETURN = "return"

    class Message:
        def __init__(self, **values):
            self.__dict__.update(values)
            self.message_type = MessageType.METHOD_RETURN
            self.body = values.get("body", [])

    class Variant:
        def __init__(self, _signature, value):
            self.value = value

    read_fd, write_fd = os.pipe()

    class FakeBus:
        unique_name = ":1.77"

        def __init__(self):
            self.handlers = []
            self.messages = []

        async def connect(self):
            return self

        def add_message_handler(self, handler):
            self.handlers.append(handler)

        def remove_message_handler(self, handler):
            self.handlers.remove(handler)

        async def call(self, message):
            self.messages.append(message)
            if message.member in {"CreateSession", "SelectSources", "Start"}:
                token = message.body[-1]["handle_token"].value
                results = {}
                if message.member == "CreateSession":
                    results["session_handle"] = Variant(
                        "o",
                        "/org/freedesktop/portal/desktop/session/1_77/dayfinch",
                    )
                elif message.member == "Start":
                    results = {
                        "restore_token": Variant("s", "rotated-token"),
                        "streams": Variant(
                            "a(ua{sv})",
                            [(42, {"pipewire-serial": Variant("t", 987)})],
                        ),
                    }
                response = SimpleNamespace(
                    message_type=MessageType.SIGNAL,
                    path=f"/org/freedesktop/portal/desktop/request/1_77/{token}",
                    interface="org.freedesktop.portal.Request",
                    member="Response",
                    body=[0, results],
                )
                for handler in list(self.handlers):
                    handler(response)
            if message.member == "OpenPipeWireRemote":
                return SimpleNamespace(
                    message_type=MessageType.METHOD_RETURN,
                    body=[0],
                    unix_fds=[read_fd],
                )
            return SimpleNamespace(message_type=MessageType.METHOD_RETURN, body=[])

        def disconnect(self):
            pass

    fake_bus = FakeBus()
    monkeypatch.setitem(
        sys.modules,
        "dbus_next",
        SimpleNamespace(
            BusType=SimpleNamespace(SESSION="session"),
            Message=Message,
            MessageType=MessageType,
            Variant=Variant,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "dbus_next.aio",
        SimpleNamespace(MessageBus=lambda **_kwargs: fake_bus),
    )
    calls = []
    saved = []

    def snapshot(fd, node_id, serial):
        calls.append((fd, node_id, serial, list(saved)))
        output = BytesIO()
        Image.new("RGB", (3, 2), "green").save(output, format="PNG")
        return output.getvalue()

    monkeypatch.setattr(capture, "_pipewire_snapshot", snapshot)
    try:
        result = asyncio.run(
            capture._request_portal_screencast_frame(
                restore_token="previous-token",
                save_restore_token=saved.append,
                all_monitors=True,
            )
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)

    with Image.open(BytesIO(result)) as image:
        assert image.size == (3, 2)
    assert saved == ["rotated-token"]
    assert calls[0][1:] == (42, 987, ["rotated-token"])
    selected = next(
        message for message in fake_bus.messages if message.member == "SelectSources"
    )
    assert selected.body[1]["persist_mode"].value == 2
    assert selected.body[1]["multiple"].value is True
    assert selected.body[1]["restore_token"].value == "previous-token"
    assert any(message.member == "Close" for message in fake_bus.messages)


def test_pipewire_snapshot_uses_restricted_fd_and_serial(tmp_path, monkeypatch):
    read_fd, write_fd = os.pipe()
    calls = []
    monkeypatch.setattr(capture.shutil, "which", lambda _name: "/usr/bin/gst-launch")

    def run(command, **kwargs):
        calls.append((command, kwargs))
        location = next(value for value in command if value.startswith("location="))
        Image.new("RGB", (4, 3), "purple").save(
            location.partition("=")[2], format="PNG"
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(capture.subprocess, "run", run)
    try:
        result = capture._pipewire_snapshot(read_fd, 12, 3456)
    finally:
        os.close(read_fd)
        os.close(write_fd)

    with Image.open(BytesIO(result)) as image:
        assert image.size == (4, 3)
    assert "target-object=3456" in calls[0][0]
    assert calls[0][1]["pass_fds"] == (read_fd,)


def test_multiple_wayland_monitor_frames_are_combined():
    frames = []
    for size, color in (((3, 2), "red"), ((2, 4), "blue")):
        output = BytesIO()
        Image.new("RGB", size, color).save(output, format="PNG")
        frames.append(output.getvalue())

    combined = capture._combine_monitor_frames(frames)

    with Image.open(BytesIO(combined)) as image:
        assert image.size == (5, 4)
        assert image.getpixel((0, 0)) == (255, 0, 0)
        assert image.getpixel((4, 0)) == (0, 0, 255)
        assert image.getpixel((0, 3)) == (0, 0, 0)

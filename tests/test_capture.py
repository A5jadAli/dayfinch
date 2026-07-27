import asyncio
import sys
from io import BytesIO
from types import SimpleNamespace

from PIL import Image

from agent import capture


def test_capture_uses_portal_on_wayland(monkeypatch):
    monkeypatch.setattr(capture, "is_wayland", lambda: True)
    monkeypatch.setattr(capture, "_capture_wayland", lambda quality: b"portal")
    monkeypatch.setattr(
        capture,
        "_capture_mss",
        lambda _all_monitors, _quality: (_ for _ in ()).throw(AssertionError()),
    )

    assert capture.capture_screenshot(all_monitors=True, jpeg_quality=65) == b"portal"


def test_capture_uses_mss_outside_wayland(monkeypatch):
    monkeypatch.setattr(capture, "is_wayland", lambda: False)
    monkeypatch.setattr(
        capture, "_capture_mss", lambda all_monitors, quality: b"desktop"
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

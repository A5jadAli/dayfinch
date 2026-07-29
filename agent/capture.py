from __future__ import annotations

import asyncio
import os
import platform
import uuid
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from PIL import Image


class CaptureUnavailable(RuntimeError):
    """Raised when the current desktop cannot provide a screenshot safely."""


def is_wayland() -> bool:
    return (
        platform.system() == "Linux"
        and os.getenv("XDG_SESSION_TYPE", "").strip().lower() == "wayland"
    )


def capture_screenshot(
    *, all_monitors: bool, jpeg_quality: int, max_dimension: int = 0
) -> bytes:
    if is_wayland():
        return _capture_wayland(jpeg_quality, max_dimension)
    return _capture_mss(all_monitors, jpeg_quality, max_dimension)


def _capture_mss(all_monitors: bool, jpeg_quality: int, max_dimension: int) -> bytes:
    try:
        import mss

        with mss.mss() as capture:
            if len(capture.monitors) < 2:
                raise CaptureUnavailable("No desktop monitor is available")
            monitor = capture.monitors[0 if all_monitors else 1]
            shot = capture.grab(monitor)
            image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    except CaptureUnavailable:
        raise
    except Exception as exc:
        raise CaptureUnavailable(
            "Desktop capture failed; check screen-recording permission"
        ) from exc
    return _as_jpeg(image, jpeg_quality, max_dimension)


def _capture_wayland(jpeg_quality: int, max_dimension: int) -> bytes:
    """Capture through the consent-aware XDG Screenshot portal."""
    try:
        screenshot_path = asyncio.run(_request_portal_screenshot())
        with Image.open(screenshot_path) as image:
            screenshot = _as_jpeg(image.convert("RGB"), jpeg_quality, max_dimension)
    except CaptureUnavailable:
        raise
    except Exception as exc:
        raise CaptureUnavailable(
            "Wayland capture failed; allow the desktop screenshot portal request"
        ) from exc
    finally:
        if "screenshot_path" in locals():
            try:
                screenshot_path.unlink(missing_ok=True)
            except OSError:
                pass
    return screenshot


async def _request_portal_screenshot() -> Path:
    try:
        from dbus_next import BusType, Message, MessageType, Variant
        from dbus_next.aio import MessageBus
    except ImportError as exc:
        raise CaptureUnavailable(
            'Wayland support is missing; install Dayfinch with the "agent" extra'
        ) from exc

    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    token = "dayfinch_" + uuid.uuid4().hex
    sender = bus.unique_name.removeprefix(":").replace(".", "_")
    request_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
    loop = asyncio.get_running_loop()
    response_future: asyncio.Future[tuple[int, dict]] = loop.create_future()

    def receive(message):
        if (
            message.message_type == MessageType.SIGNAL
            and message.path == request_path
            and message.interface == "org.freedesktop.portal.Request"
            and message.member == "Response"
            and not response_future.done()
        ):
            response_future.set_result((message.body[0], message.body[1]))
        return False

    bus.add_message_handler(receive)
    match_rule = (
        "type='signal',interface='org.freedesktop.portal.Request',"
        f"member='Response',path='{request_path}'"
    )
    try:
        match_reply = await bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="AddMatch",
                signature="s",
                body=[match_rule],
            )
        )
        if match_reply.message_type == MessageType.ERROR:
            raise CaptureUnavailable("Unable to subscribe to the desktop portal")

        reply = await bus.call(
            Message(
                destination="org.freedesktop.portal.Desktop",
                path="/org/freedesktop/portal/desktop",
                interface="org.freedesktop.portal.Screenshot",
                member="Screenshot",
                signature="sa{sv}",
                body=[
                    "",
                    {
                        "handle_token": Variant("s", token),
                        "interactive": Variant("b", False),
                        "modal": Variant("b", False),
                    },
                ],
            )
        )
        if reply.message_type == MessageType.ERROR:
            detail = reply.body[0] if reply.body else "portal unavailable"
            raise CaptureUnavailable(
                f"Screenshot portal rejected the request: {detail}"
            )

        response_code, results = await asyncio.wait_for(response_future, timeout=120)
        if response_code != 0:
            raise CaptureUnavailable("Screenshot request was cancelled or denied")
        uri = results.get("uri")
        uri_value = uri.value if uri is not None else ""
        parsed = urlparse(uri_value)
        if parsed.scheme != "file":
            raise CaptureUnavailable("Screenshot portal returned an unsupported URI")
        path = Path(url2pathname(unquote(parsed.path)))
        if not path.is_file():
            raise CaptureUnavailable("Screenshot portal output is not accessible")
        return path
    finally:
        bus.disconnect()


def downscale(image: Image.Image, max_dimension: int) -> Image.Image:
    """Shrink by a whole-number factor so captures stay cheap on modest hardware.

    Box reduction costs a fraction of resampling and avoids holding a full 4K
    RGB buffer through JPEG encoding, which is what makes the agent noticeable
    on low-end machines.
    """
    if max_dimension <= 0:
        return image
    longest = max(image.size)
    if longest <= max_dimension:
        return image
    factor = -(-longest // max_dimension)  # ceil, so the result fits the limit
    return image.reduce(factor)


def _as_jpeg(image: Image.Image, jpeg_quality: int, max_dimension: int = 0) -> bytes:
    output = BytesIO()
    # optimize=True costs roughly twice the CPU for a few percent of size.
    downscale(image, max_dimension).save(output, format="JPEG", quality=jpeg_quality)
    return output.getvalue()

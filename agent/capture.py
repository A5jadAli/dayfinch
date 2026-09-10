from __future__ import annotations

import asyncio
import os
import platform
import shutil
import subprocess
import tempfile
import threading
import uuid
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from PIL import Image, ImageFilter


class CaptureUnavailable(RuntimeError):
    """Raised when the current desktop cannot provide a screenshot safely."""


_WAYLAND_CAPTURE_LOCK = threading.Lock()


def is_wayland() -> bool:
    return (
        platform.system() == "Linux"
        and os.getenv("XDG_SESSION_TYPE", "").strip().lower() == "wayland"
    )


def capture_screenshot(
    *,
    all_monitors: bool,
    jpeg_quality: int,
    max_dimension: int = 0,
    wayland_restore_token: str = "",
    save_wayland_restore_token: Callable[[str], None] | None = None,
) -> bytes:
    if is_wayland():
        if save_wayland_restore_token is None:
            return _capture_wayland(jpeg_quality, max_dimension)
        return _capture_wayland(
            jpeg_quality,
            max_dimension,
            all_monitors=all_monitors,
            restore_token=wayland_restore_token,
            save_restore_token=save_wayland_restore_token,
        )
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


def _capture_wayland(
    jpeg_quality: int,
    max_dimension: int,
    *,
    all_monitors: bool = False,
    restore_token: str = "",
    save_restore_token: Callable[[str], None] | None = None,
) -> bytes:
    """Capture through a persistent ScreenCast session or Screenshot fallback."""
    if save_restore_token is not None:
        try:
            with _WAYLAND_CAPTURE_LOCK:
                image_data = asyncio.run(
                    _request_portal_screencast_frame(
                        all_monitors=all_monitors,
                        restore_token=restore_token,
                        save_restore_token=save_restore_token,
                    )
                )
            with Image.open(BytesIO(image_data)) as image:
                return _as_jpeg(image.convert("RGB"), jpeg_quality, max_dimension)
        except CaptureUnavailable:
            raise
        except Exception as exc:
            raise CaptureUnavailable(
                "Wayland ScreenCast failed; check portal and PipeWire permissions"
            ) from exc

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


def _variant_value(values: dict, name: str, default=None):
    value = values.get(name)
    return getattr(value, "value", default) if value is not None else default


async def _portal_response(bus, message, request_path: str) -> dict:
    from dbus_next import Message, MessageType

    loop = asyncio.get_running_loop()
    response_future: asyncio.Future[tuple[int, dict]] = loop.create_future()

    def receive(response):
        if (
            response.message_type == MessageType.SIGNAL
            and response.path == request_path
            and response.interface == "org.freedesktop.portal.Request"
            and response.member == "Response"
            and not response_future.done()
        ):
            response_future.set_result((response.body[0], response.body[1]))
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
        reply = await bus.call(message)
        if reply.message_type == MessageType.ERROR:
            raise CaptureUnavailable("ScreenCast portal rejected the request")
        response_code, results = await asyncio.wait_for(response_future, timeout=120)
        if response_code != 0:
            raise CaptureUnavailable("ScreenCast request was cancelled or denied")
        return results
    finally:
        remove = getattr(bus, "remove_message_handler", None)
        if remove is not None:
            remove(receive)


def _request_path(bus, token: str) -> str:
    sender = bus.unique_name.removeprefix(":").replace(".", "_")
    return f"/org/freedesktop/portal/desktop/request/{sender}/{token}"


def _pipewire_snapshot(fd: int, node_id: int, pipewire_serial: int | None) -> bytes:
    launcher = shutil.which("gst-launch-1.0")
    if not launcher:
        raise CaptureUnavailable(
            "Persistent Wayland capture requires GStreamer PipeWire support"
        )
    target = (
        f"target-object={pipewire_serial}"
        if pipewire_serial is not None
        else f"path={node_id}"
    )
    with tempfile.TemporaryDirectory(prefix="dayfinch-wayland-") as directory:
        output = Path(directory) / "capture.png"
        try:
            completed = subprocess.run(
                [
                    launcher,
                    "-q",
                    "pipewiresrc",
                    f"fd={fd}",
                    target,
                    "num-buffers=1",
                    "!",
                    "videoconvert",
                    "!",
                    "pngenc",
                    "snapshot=true",
                    "!",
                    "filesink",
                    f"location={output}",
                ],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                pass_fds=(fd,),
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CaptureUnavailable("PipeWire frame capture did not complete") from exc
        if (
            completed.returncode != 0
            or not output.is_file()
            or output.stat().st_size < 1
        ):
            raise CaptureUnavailable("GStreamer could not read the shared monitor")
        return output.read_bytes()


def _combine_monitor_frames(frames: list[bytes]) -> bytes:
    if not frames:
        raise CaptureUnavailable("ScreenCast returned no monitor frames")
    if len(frames) == 1:
        return frames[0]
    images: list[Image.Image] = []
    try:
        for frame in frames:
            with Image.open(BytesIO(frame)) as source:
                images.append(source.convert("RGB"))
        width = sum(image.width for image in images)
        height = max(image.height for image in images)
        if width < 1 or height < 1 or width * height > 200_000_000:
            raise CaptureUnavailable("Combined monitor capture is too large")
        combined = Image.new("RGB", (width, height), "black")
        left = 0
        for image in images:
            combined.paste(image, (left, 0))
            left += image.width
        output = BytesIO()
        combined.save(output, format="PNG")
        return output.getvalue()
    finally:
        for image in images:
            image.close()


async def _request_portal_screencast_frame(
    *,
    restore_token: str,
    save_restore_token: Callable[[str], None],
    all_monitors: bool = False,
) -> bytes:
    try:
        from dbus_next import BusType, Message, MessageType, Variant
        from dbus_next.aio import MessageBus
    except ImportError as exc:
        raise CaptureUnavailable(
            'Wayland support is missing; install Dayfinch with the "agent" extra'
        ) from exc

    bus = await MessageBus(bus_type=BusType.SESSION, negotiate_unix_fd=True).connect()
    session_handle = ""
    pipewire_fd = -1
    try:
        create_token = "dayfinch_create_" + uuid.uuid4().hex
        session_token = "dayfinch_session_" + uuid.uuid4().hex
        create_results = await _portal_response(
            bus,
            Message(
                destination="org.freedesktop.portal.Desktop",
                path="/org/freedesktop/portal/desktop",
                interface="org.freedesktop.portal.ScreenCast",
                member="CreateSession",
                signature="a{sv}",
                body=[
                    {
                        "handle_token": Variant("s", create_token),
                        "session_handle_token": Variant("s", session_token),
                    }
                ],
            ),
            _request_path(bus, create_token),
        )
        session_handle = str(_variant_value(create_results, "session_handle", ""))
        if not session_handle.startswith("/org/freedesktop/portal/desktop/session/"):
            raise CaptureUnavailable("ScreenCast portal returned an invalid session")

        select_token = "dayfinch_select_" + uuid.uuid4().hex
        source_options = {
            "handle_token": Variant("s", select_token),
            "types": Variant("u", 1),
            "multiple": Variant("b", all_monitors),
            "persist_mode": Variant("u", 2),
        }
        if restore_token:
            source_options["restore_token"] = Variant("s", restore_token[:4096])
        await _portal_response(
            bus,
            Message(
                destination="org.freedesktop.portal.Desktop",
                path="/org/freedesktop/portal/desktop",
                interface="org.freedesktop.portal.ScreenCast",
                member="SelectSources",
                signature="oa{sv}",
                body=[session_handle, source_options],
            ),
            _request_path(bus, select_token),
        )

        start_token = "dayfinch_start_" + uuid.uuid4().hex
        start_results = await _portal_response(
            bus,
            Message(
                destination="org.freedesktop.portal.Desktop",
                path="/org/freedesktop/portal/desktop",
                interface="org.freedesktop.portal.ScreenCast",
                member="Start",
                signature="osa{sv}",
                body=[
                    session_handle,
                    "",
                    {"handle_token": Variant("s", start_token)},
                ],
            ),
            _request_path(bus, start_token),
        )
        new_restore_token = str(_variant_value(start_results, "restore_token", ""))
        if len(new_restore_token) > 4096:
            raise CaptureUnavailable("ScreenCast restore token is unexpectedly large")
        # Tokens are single-use. Persist the replacement before attempting frame
        # conversion so a transient GStreamer failure does not force a new prompt.
        save_restore_token(new_restore_token)
        streams = _variant_value(start_results, "streams", [])
        if not isinstance(streams, list) or not streams:
            raise CaptureUnavailable("No monitor was selected for ScreenCast")
        if all_monitors and len(streams) > 8:
            raise CaptureUnavailable("ScreenCast selected too many monitors")
        selected_streams = streams[:8] if all_monitors else streams[:1]
        frames = []
        for stream in selected_streams:
            if not isinstance(stream, (list, tuple)) or len(stream) != 2:
                raise CaptureUnavailable("ScreenCast returned invalid stream metadata")
            node_id, properties = stream
            if not isinstance(node_id, int) or not isinstance(properties, dict):
                raise CaptureUnavailable("ScreenCast returned invalid stream metadata")
            serial = _variant_value(properties, "pipewire-serial")
            pipewire_serial = serial if isinstance(serial, int) else None

            remote = await bus.call(
                Message(
                    destination="org.freedesktop.portal.Desktop",
                    path="/org/freedesktop/portal/desktop",
                    interface="org.freedesktop.portal.ScreenCast",
                    member="OpenPipeWireRemote",
                    signature="oa{sv}",
                    body=[session_handle, {}],
                )
            )
            if remote.message_type == MessageType.ERROR or not remote.body:
                raise CaptureUnavailable(
                    "ScreenCast could not open its PipeWire remote"
                )
            descriptor_index = int(remote.body[0])
            descriptors = getattr(remote, "unix_fds", [])
            if descriptor_index < 0 or descriptor_index >= len(descriptors):
                raise CaptureUnavailable(
                    "ScreenCast returned an invalid PipeWire handle"
                )
            pipewire_fd = os.dup(descriptors[descriptor_index])
            try:
                frames.append(_pipewire_snapshot(pipewire_fd, node_id, pipewire_serial))
            finally:
                os.close(pipewire_fd)
                pipewire_fd = -1
        return _combine_monitor_frames(frames)
    finally:
        if pipewire_fd >= 0:
            os.close(pipewire_fd)
        if session_handle:
            try:
                await bus.call(
                    Message(
                        destination="org.freedesktop.portal.Desktop",
                        path=session_handle,
                        interface="org.freedesktop.portal.Session",
                        member="Close",
                    )
                )
            except Exception:
                pass
        bus.disconnect()


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


def blur_screenshot(data: bytes, jpeg_quality: int = 65) -> bytes:
    """Irreversibly obscure text and fine detail before a capture leaves the device."""
    with Image.open(BytesIO(data)) as image:
        blurred = image.convert("RGB").filter(ImageFilter.GaussianBlur(radius=12))
        return _as_jpeg(blurred, jpeg_quality)

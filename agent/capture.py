from __future__ import annotations

from io import BytesIO

from PIL import Image


def capture_screenshot(*, all_monitors: bool, jpeg_quality: int) -> bytes:
    import mss

    with mss.mss() as capture:
        monitor = capture.monitors[0 if all_monitors else 1]
        shot = capture.grab(monitor)
        image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        output = BytesIO()
        image.save(output, format="JPEG", quality=jpeg_quality, optimize=True)
        return output.getvalue()

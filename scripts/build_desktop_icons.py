from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


def _icon(size: int = 1024) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    radius = size // 4
    draw.rounded_rectangle(
        (0, 0, size - 1, size - 1),
        radius=radius,
        fill=(16, 17, 22, 255),
    )
    # The three swept quarter-discs mirror the web mark without requiring an SVG
    # renderer in the release runner.
    purple = (123, 103, 240, 255)
    center = size // 2
    outer = size * 13 // 52
    box = (center - outer, center - outer, center + outer, center + outer)
    draw.pieslice(box, 270, 360, fill=purple)
    draw.pieslice(box, 0, 90, fill=purple)
    draw.pieslice(box, 180, 270, fill=purple)
    return image


def build_icons(output_directory: Path) -> tuple[Path, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    image = _icon()
    windows = output_directory / "dayfinch-agent.ico"
    macos = output_directory / "dayfinch-agent.icns"
    image.save(
        windows,
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    image.save(macos, format="ICNS")
    return windows, macos


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build deterministic Dayfinch Windows and macOS icons"
    )
    parser.add_argument("--output-dir", type=Path, default=Path("build/icons"))
    arguments = parser.parse_args()
    for path in build_icons(arguments.output_dir):
        print(path)


if __name__ == "__main__":
    main()

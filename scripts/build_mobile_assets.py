from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


def build_mobile_assets(output_directory: Path) -> tuple[Path, Path, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    size = 1024
    image = Image.new("RGBA", (size, size), (16, 17, 22, 255))
    draw = ImageDraw.Draw(image)
    purple = (123, 103, 240, 255)
    center = size // 2
    outer = size * 13 // 52
    box = (center - outer, center - outer, center + outer, center + outer)
    draw.pieslice(box, 270, 360, fill=purple)
    draw.pieslice(box, 0, 90, fill=purple)
    draw.pieslice(box, 180, 270, fill=purple)

    icon = output_directory / "icon.png"
    adaptive = output_directory / "adaptive-icon.png"
    notification = output_directory / "notification-icon.png"
    image.save(icon, format="PNG", optimize=True)

    adaptive_image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    adaptive_image.alpha_composite(image.resize((640, 640)), (192, 192))
    adaptive_image.save(adaptive, format="PNG", optimize=True)

    mask = Image.new("RGBA", (96, 96), (0, 0, 0, 0))
    mask_draw = ImageDraw.Draw(mask)
    mask_box = (24, 24, 72, 72)
    mask_draw.pieslice(mask_box, 270, 360, fill=(255, 255, 255, 255))
    mask_draw.pieslice(mask_box, 0, 90, fill=(255, 255, 255, 255))
    mask_draw.pieslice(mask_box, 180, 270, fill=(255, 255, 255, 255))
    mask.save(notification, format="PNG", optimize=True)
    return icon, adaptive, notification


def main() -> None:
    parser = argparse.ArgumentParser(description="Build deterministic mobile icons")
    parser.add_argument("--output-dir", type=Path, default=Path("mobile/assets"))
    arguments = parser.parse_args()
    for path in build_mobile_assets(arguments.output_dir):
        print(path)


if __name__ == "__main__":
    main()

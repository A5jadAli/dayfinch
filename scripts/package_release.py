from __future__ import annotations

import argparse
import hashlib
import shutil
from pathlib import Path


def package(source: Path, target: Path) -> None:
    if not source.is_file():
        raise SystemExit(f"Packaged executable not found: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_name(f"{target.name}.sha256").write_text(
        f"{digest}  {target.name}\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    arguments = parser.parse_args()
    package(arguments.source, arguments.target)


if __name__ == "__main__":
    main()

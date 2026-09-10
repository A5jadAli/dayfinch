from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INNO_SCRIPT = PROJECT_ROOT / "packaging/windows/dayfinch-agent.iss"


class WindowsPackageError(RuntimeError):
    pass


def _regular_file(path: Path, label: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise WindowsPackageError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise WindowsPackageError(f"{label} must be a regular non-symlink file")
    if metadata.st_size < 1:
        raise WindowsPackageError(f"{label} is empty")
    return path.resolve()


def _version(value: str) -> str:
    normalized = value.strip()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9]+)?", normalized):
        raise WindowsPackageError("Windows installer version is invalid")
    return normalized


def _compiler(explicit: Path | None = None) -> Path:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    located = shutil.which("ISCC.exe") or shutil.which("iscc")
    if located:
        candidates.append(Path(located))
    for variable in ("ProgramFiles(x86)", "ProgramFiles"):
        root = os.environ.get(variable)
        if root:
            candidates.append(Path(root) / "Inno Setup 6" / "ISCC.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise WindowsPackageError("Inno Setup 6 ISCC.exe is required")


def build_installer(
    executable: Path,
    output: Path,
    version: str,
    *,
    compiler: Path | None = None,
) -> Path:
    source = _regular_file(executable, "Packaged Dayfinch executable")
    script = _regular_file(INNO_SCRIPT, "Inno Setup definition")
    package_version = _version(version)
    if output.suffix.lower() != ".exe":
        raise WindowsPackageError("Windows installer output must end in .exe")
    if output.is_symlink():
        raise WindowsPackageError("Windows installer output cannot be a symlink")
    output.parent.mkdir(parents=True, exist_ok=True)
    output_base = output.stem
    command = [
        str(_compiler(compiler)),
        f"/DAppVersion={package_version}",
        f"/DSourceExe={source}",
        f"/DOutputDir={output.parent.resolve()}",
        f"/DOutputBaseName={output_base}",
        str(script),
    ]
    completed = subprocess.run(
        command, check=False, capture_output=True, text=True, timeout=300
    )
    if completed.returncode != 0:
        raise WindowsPackageError("Inno Setup could not build the Windows installer")
    built = _regular_file(output, "Built Windows installer")
    digest = hashlib.sha256(built.read_bytes()).hexdigest()
    checksum = output.with_name(f"{output.name}.sha256")
    checksum.write_text(f"{digest}  {output.name}\n", encoding="ascii")
    return built


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a native Dayfinch Windows installer"
    )
    parser.add_argument("executable", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--compiler", type=Path)
    arguments = parser.parse_args()
    try:
        result = build_installer(
            arguments.executable,
            arguments.output,
            arguments.version,
            compiler=arguments.compiler,
        )
    except WindowsPackageError as exc:
        raise SystemExit(str(exc)) from exc
    print(result)


if __name__ == "__main__":
    main()

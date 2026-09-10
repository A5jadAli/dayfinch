from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DESKTOP_FILE = PROJECT_ROOT / "packaging/linux/dayfinch-agent.desktop"
ICON_FILE = PROJECT_ROOT / "ui/static/dayfinch-icon.svg"
LICENSE_FILE = PROJECT_ROOT / "LICENSE"
PACKAGE_NAME = "dayfinch-agent"
DEFAULT_SOURCE_DATE_EPOCH = "946684800"


class LinuxPackageError(RuntimeError):
    pass


def _regular_file(path: Path, label: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise LinuxPackageError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise LinuxPackageError(f"{label} must be a regular non-symlink file")
    if metadata.st_size < 1:
        raise LinuxPackageError(f"{label} is empty")
    return path.resolve()


def _validate_version(version: str) -> str:
    normalized = version.strip()
    if not re.fullmatch(r"[0-9][0-9A-Za-z.+:~_-]{0,79}", normalized):
        raise LinuxPackageError("Package version is invalid")
    return normalized


def _set_reproducible_times(root: Path, timestamp: int) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir():
            path.chmod(0o755)
        os.utime(path, (timestamp, timestamp), follow_symlinks=False)
    root.chmod(0o755)
    os.utime(root, (timestamp, timestamp), follow_symlinks=False)


def build_deb(
    executable: Path,
    output: Path,
    version: str,
    *,
    architecture: str = "amd64",
) -> Path:
    binary = _regular_file(executable, "Packaged Dayfinch executable")
    desktop = _regular_file(DESKTOP_FILE, "Desktop entry")
    icon = _regular_file(ICON_FILE, "Dayfinch icon")
    license_file = _regular_file(LICENSE_FILE, "Project license")
    package_version = _validate_version(version)
    if architecture not in {"amd64", "arm64"}:
        raise LinuxPackageError("Package architecture must be amd64 or arm64")
    if output.suffix != ".deb":
        raise LinuxPackageError("Linux package output must end in .deb")
    if output.is_symlink():
        raise LinuxPackageError("Linux package output cannot be a symlink")
    dpkg_deb = shutil.which("dpkg-deb")
    if not dpkg_deb:
        raise LinuxPackageError("dpkg-deb is required to build the Linux installer")

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dayfinch-deb-") as temporary_name:
        root = Path(temporary_name) / f"{PACKAGE_NAME}_{package_version}_{architecture}"
        control_dir = root / "DEBIAN"
        binary_dir = root / "usr/bin"
        application_dir = root / "usr/share/applications"
        icon_dir = root / "usr/share/icons/hicolor/scalable/apps"
        documentation_dir = root / "usr/share/doc/dayfinch-agent"
        for directory in (
            control_dir,
            binary_dir,
            application_dir,
            icon_dir,
            documentation_dir,
        ):
            directory.mkdir(parents=True, mode=0o755)

        installed_binary = binary_dir / "dayfinch-agent"
        shutil.copyfile(binary, installed_binary)
        installed_binary.chmod(0o755)
        installed_desktop = application_dir / "dayfinch-agent.desktop"
        shutil.copyfile(desktop, installed_desktop)
        installed_desktop.chmod(0o644)
        installed_icon = icon_dir / "dayfinch-agent.svg"
        shutil.copyfile(icon, installed_icon)
        installed_icon.chmod(0o644)
        installed_license = documentation_dir / "copyright"
        shutil.copyfile(license_file, installed_license)
        installed_license.chmod(0o644)

        installed_kib = max(1, (binary.stat().st_size + 1023) // 1024)
        control = control_dir / "control"
        control.write_text(
            "\n".join(
                (
                    f"Package: {PACKAGE_NAME}",
                    f"Version: {package_version}",
                    "Section: utils",
                    "Priority: optional",
                    f"Architecture: {architecture}",
                    "Maintainer: Dayfinch Operations <operations@example.invalid>",
                    "Depends: libc6, libx11-6, libxtst6, libxcb1, xdg-desktop-portal, "
                    "pipewire, gstreamer1.0-tools, gstreamer1.0-pipewire, "
                    "gstreamer1.0-plugins-base, gstreamer1.0-plugins-good",
                    f"Installed-Size: {installed_kib}",
                    "Description: Dayfinch visible employee tracker",
                    " Tracks time only after an employee explicitly starts the visible timer.",
                    " Screenshots and aggregate activity follow the disclosed organization policy.",
                    "",
                )
            ),
            encoding="utf-8",
        )
        control.chmod(0o644)

        source_date_epoch = os.environ.get(
            "SOURCE_DATE_EPOCH", DEFAULT_SOURCE_DATE_EPOCH
        )
        try:
            timestamp = int(source_date_epoch)
        except ValueError as exc:
            raise LinuxPackageError("SOURCE_DATE_EPOCH must be an integer") from exc
        if timestamp < 0:
            raise LinuxPackageError("SOURCE_DATE_EPOCH cannot be negative")
        _set_reproducible_times(root, timestamp)

        temporary_output = output.parent / f".{output.name}.{os.getpid()}.part"
        try:
            completed = subprocess.run(
                [
                    dpkg_deb,
                    "--root-owner-group",
                    "--build",
                    "--uniform-compression",
                    "-Zxz",
                    "-z9",
                    str(root),
                    str(temporary_output),
                ],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "SOURCE_DATE_EPOCH": str(timestamp)},
            )
            if completed.returncode != 0:
                raise LinuxPackageError("dpkg-deb could not build the Linux installer")
            _regular_file(temporary_output, "Built Linux installer")
            os.replace(temporary_output, output)
        finally:
            temporary_output.unlink(missing_ok=True)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum = output.with_name(f"{output.name}.sha256")
    temporary_checksum = checksum.with_name(f".{checksum.name}.{os.getpid()}.part")
    try:
        temporary_checksum.write_text(f"{digest}  {output.name}\n", encoding="ascii")
        os.replace(temporary_checksum, checksum)
    finally:
        temporary_checksum.unlink(missing_ok=True)
    return output.resolve()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a reproducible native Dayfinch Linux package"
    )
    parser.add_argument("executable", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--architecture", choices=("amd64", "arm64"), default="amd64")
    arguments = parser.parse_args()
    try:
        package = build_deb(
            arguments.executable,
            arguments.output,
            arguments.version,
            architecture=arguments.architecture,
        )
    except LinuxPackageError as exc:
        raise SystemExit(str(exc)) from exc
    print(package)


if __name__ == "__main__":
    main()

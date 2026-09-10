from __future__ import annotations

import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from scripts import package_macos, package_windows
from scripts.build_desktop_icons import build_icons
from scripts.package_macos import MacOSPackageError, build_pkg, stage_app
from scripts.package_windows import WindowsPackageError, build_installer


def _file(path: Path, content: bytes = b"binary") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_desktop_icons_are_valid_native_formats(tmp_path):
    windows, macos = build_icons(tmp_path / "icons")

    with Image.open(windows) as image:
        assert image.format == "ICO"
        assert image.size == (256, 256)
    with Image.open(macos) as image:
        assert image.format == "ICNS"
        assert image.size == (1024, 1024)


def test_macos_app_staging_has_privacy_metadata_and_executable(tmp_path):
    executable = _file(tmp_path / "Dayfinch-Agent")
    _, icon = build_icons(tmp_path / "icons")

    app = stage_app(executable, tmp_path / "Dayfinch Tracker.app", "1.2.3", icon=icon)
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    installed = app / "Contents/MacOS/Dayfinch-Agent"

    assert info["CFBundleIdentifier"] == "com.dayfinch.tracker"
    assert info["CFBundleShortVersionString"] == "1.2.3"
    assert info["LSUIElement"] is True
    assert "screenshots" in info["NSScreenCaptureUsageDescription"]
    assert installed.read_bytes() == b"binary"
    assert installed.stat().st_mode & 0o111
    assert (app / "Contents/Resources/Dayfinch.icns").is_file()


def test_macos_package_is_signed_built_and_checksummed(tmp_path, monkeypatch):
    executable = _file(tmp_path / "Dayfinch-Agent")
    _, icon = build_icons(tmp_path / "icons")
    output = tmp_path / "release/dayfinch-agent.pkg"
    calls: list[list[str]] = []

    monkeypatch.setattr(package_macos, "_tool", lambda name: name)

    def run(command, **_kwargs):
        calls.append(command)
        if command[0] == "pkgbuild":
            _file(Path(command[-1]), b"native package")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(package_macos.subprocess, "run", run)
    result = build_pkg(executable, output, "1.2.3", icon=icon)

    assert result == output.resolve()
    assert output.read_bytes() == b"native package"
    assert (
        (output.parent / f"{output.name}.sha256")
        .read_text()
        .endswith(f"  {output.name}\n")
    )
    assert calls[0][0:6] == [
        "codesign",
        "--force",
        "--deep",
        "--options",
        "runtime",
        "--sign",
    ]
    pkgbuild = next(command for command in calls if command[0] == "pkgbuild")
    assert pkgbuild[pkgbuild.index("--install-location") + 1] == "/Applications"


def test_macos_notarization_requires_complete_credentials(tmp_path):
    executable = _file(tmp_path / "Dayfinch-Agent")

    with pytest.raises(MacOSPackageError, match="All App Store Connect"):
        build_pkg(
            executable,
            tmp_path / "dayfinch.pkg",
            "1.2.3",
            icon=None,
            notary_key=tmp_path / "AuthKey.p8",
        )


def test_package_versions_and_existing_app_are_rejected(tmp_path):
    executable = _file(tmp_path / "Dayfinch-Agent")
    destination = tmp_path / "Dayfinch Tracker.app"
    destination.mkdir()

    with pytest.raises(MacOSPackageError, match="destination already exists"):
        stage_app(executable, destination, "1.2.3", icon=None)
    with pytest.raises(MacOSPackageError, match="version is invalid"):
        stage_app(executable, tmp_path / "other.app", "1.2.3-beta", icon=None)
    with pytest.raises(WindowsPackageError, match="version is invalid"):
        build_installer(executable, tmp_path / "setup.exe", "latest")


def test_windows_inno_installer_is_built_and_checksummed(tmp_path, monkeypatch):
    executable = _file(tmp_path / "Dayfinch-Agent.exe")
    compiler = _file(tmp_path / "ISCC.exe")
    output = tmp_path / "release/dayfinch-agent-setup.exe"
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        output_dir = Path(
            next(value for value in command if value.startswith("/DOutputDir=")).split(
                "=", 1
            )[1]
        )
        output_name = next(
            value for value in command if value.startswith("/DOutputBaseName=")
        ).split("=", 1)[1]
        _file(output_dir / f"{output_name}.exe", b"windows installer")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(package_windows.subprocess, "run", run)
    result = build_installer(executable, output, "1.2.3", compiler=compiler)

    assert result == output.resolve()
    assert output.read_bytes() == b"windows installer"
    assert calls[0][0] == str(compiler.resolve())
    assert "/DAppVersion=1.2.3" in calls[0]
    assert output.with_name(f"{output.name}.sha256").is_file()


def test_desktop_packagers_reject_symlink_inputs(tmp_path):
    source = _file(tmp_path / "source", b"payload")
    link = tmp_path / "link"
    link.symlink_to(source)

    with pytest.raises(MacOSPackageError, match="non-symlink"):
        stage_app(link, tmp_path / "app", "1.2.3", icon=None)
    with pytest.raises(WindowsPackageError, match="non-symlink"):
        build_installer(link, tmp_path / "setup.exe", "1.2.3")

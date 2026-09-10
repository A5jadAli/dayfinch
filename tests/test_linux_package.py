from __future__ import annotations

import hashlib
import shutil
import stat
import subprocess

import pytest

from scripts.package_linux import LinuxPackageError, build_deb


@pytest.mark.skipif(shutil.which("dpkg-deb") is None, reason="dpkg-deb unavailable")
def test_linux_package_contains_private_tracker_launcher_and_metadata(tmp_path):
    executable = tmp_path / "Dayfinch-Agent"
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    package = tmp_path / "release/dayfinch-agent_0.6.0_amd64.deb"

    built = build_deb(executable, package, "0.6.0")

    assert built == package.resolve()
    fields = subprocess.run(
        [
            "dpkg-deb",
            "--show",
            "--showformat=${Package} ${Version} ${Architecture}",
            str(package),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert fields == "dayfinch-agent 0.6.0 amd64"
    dependencies = subprocess.run(
        ["dpkg-deb", "--field", str(package), "Depends"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "gstreamer1.0-pipewire" in dependencies
    assert "gstreamer1.0-plugins-good" in dependencies
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    assert package.with_name(f"{package.name}.sha256").read_text() == (
        f"{digest}  {package.name}\n"
    )
    extracted = tmp_path / "extracted"
    subprocess.run(["dpkg-deb", "--extract", str(package), str(extracted)], check=True)
    installed = extracted / "usr/bin/dayfinch-agent"
    launcher = extracted / "usr/share/applications/dayfinch-agent.desktop"
    icon = extracted / "usr/share/icons/hicolor/scalable/apps/dayfinch-agent.svg"
    license_file = extracted / "usr/share/doc/dayfinch-agent/copyright"
    assert stat.S_IMODE(installed.stat().st_mode) == 0o755
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o755
        for path in extracted.rglob("*")
        if path.is_dir()
    )
    assert "Exec=dayfinch-agent" in launcher.read_text(encoding="utf-8")
    assert "Dayfinch" in icon.read_text(encoding="utf-8")
    assert "MIT License" in license_file.read_text(encoding="utf-8")


def test_linux_package_rejects_unsafe_inputs(tmp_path):
    executable = tmp_path / "agent"
    executable.write_bytes(b"agent")
    link = tmp_path / "agent-link"
    link.symlink_to(executable)
    with pytest.raises(LinuxPackageError, match="non-symlink"):
        build_deb(link, tmp_path / "agent.deb", "0.6.0")
    with pytest.raises(LinuxPackageError, match="version"):
        build_deb(executable, tmp_path / "agent.deb", "../../bad")
    with pytest.raises(LinuxPackageError, match="end in .deb"):
        build_deb(executable, tmp_path / "agent.zip", "0.6.0")


@pytest.mark.skipif(shutil.which("dpkg-deb") is None, reason="dpkg-deb unavailable")
def test_linux_package_build_is_reproducible(tmp_path, monkeypatch):
    executable = tmp_path / "agent"
    executable.write_bytes(b"deterministic-agent-binary")
    executable.chmod(0o755)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1767225600")
    first = tmp_path / "first.deb"
    second = tmp_path / "second.deb"

    build_deb(executable, first, "1.2.3")
    build_deb(executable, second, "1.2.3")

    assert first.read_bytes() == second.read_bytes()
    assert (
        first.with_name(f"{first.name}.sha256").read_text().split()[0]
        == (second.with_name(f"{second.name}.sha256").read_text().split()[0])
    )

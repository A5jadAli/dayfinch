from __future__ import annotations

import argparse
import hashlib
import os
import plistlib
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ICON = PROJECT_ROOT / "build/icons/dayfinch-agent.icns"
APP_NAME = "Dayfinch Tracker.app"
BUNDLE_IDENTIFIER = "com.dayfinch.tracker"


class MacOSPackageError(RuntimeError):
    pass


def _regular_file(path: Path, label: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise MacOSPackageError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise MacOSPackageError(f"{label} must be a regular non-symlink file")
    if metadata.st_size < 1:
        raise MacOSPackageError(f"{label} is empty")
    return path.resolve()


def _version(value: str) -> str:
    normalized = value.strip()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9]+)?", normalized):
        raise MacOSPackageError("macOS package version is invalid")
    return normalized


def stage_app(
    executable: Path,
    destination: Path,
    version: str,
    *,
    icon: Path | None = DEFAULT_ICON,
) -> Path:
    source = _regular_file(executable, "Packaged Dayfinch executable")
    package_version = _version(version)
    if destination.exists():
        raise MacOSPackageError("macOS application staging destination already exists")
    contents = destination / "Contents"
    executable_dir = contents / "MacOS"
    resources = contents / "Resources"
    executable_dir.mkdir(parents=True, mode=0o755)
    resources.mkdir(parents=True, mode=0o755)
    installed = executable_dir / "Dayfinch-Agent"
    shutil.copyfile(source, installed)
    installed.chmod(0o755)

    payload: dict[str, object] = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": "Dayfinch Tracker",
        "CFBundleExecutable": "Dayfinch-Agent",
        "CFBundleIdentifier": BUNDLE_IDENTIFIER,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "Dayfinch Tracker",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": package_version,
        "CFBundleVersion": package_version,
        "LSMinimumSystemVersion": "12.0",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "NSScreenCaptureUsageDescription": (
            "Dayfinch captures policy-controlled screenshots only while you are "
            "visibly tracking time."
        ),
    }
    if icon is not None:
        icon_source = _regular_file(icon, "Dayfinch macOS icon")
        installed_icon = resources / "Dayfinch.icns"
        shutil.copyfile(icon_source, installed_icon)
        installed_icon.chmod(0o644)
        payload["CFBundleIconFile"] = installed_icon.name
    info = contents / "Info.plist"
    with info.open("wb") as stream:
        plistlib.dump(payload, stream, fmt=plistlib.FMT_XML, sort_keys=True)
    info.chmod(0o644)
    return destination.resolve()


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise MacOSPackageError(f"{name} is required to build the macOS installer")
    return path


def _run(command: list[str], label: str) -> None:
    completed = subprocess.run(
        command, check=False, capture_output=True, text=True, timeout=1800
    )
    if completed.returncode != 0:
        raise MacOSPackageError(f"{label} failed")


def build_pkg(
    executable: Path,
    output: Path,
    version: str,
    *,
    icon: Path | None = DEFAULT_ICON,
    application_identity: str = "",
    installer_identity: str = "",
    notary_key: Path | None = None,
    notary_key_id: str = "",
    notary_issuer: str = "",
) -> Path:
    package_version = _version(version)
    if output.suffix.lower() != ".pkg":
        raise MacOSPackageError("macOS installer output must end in .pkg")
    if output.is_symlink():
        raise MacOSPackageError("macOS installer output cannot be a symlink")
    notary_values = (notary_key, notary_key_id.strip(), notary_issuer.strip())
    if any(notary_values) and not all(notary_values):
        raise MacOSPackageError(
            "All App Store Connect notarization values are required"
        )
    if all(notary_values) and (not application_identity or not installer_identity):
        raise MacOSPackageError("Notarization requires both Developer ID identities")

    codesign = _tool("codesign")
    pkgbuild = _tool("pkgbuild")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output.parent / f".{output.name}.{os.getpid()}.part.pkg"
    with tempfile.TemporaryDirectory(prefix="dayfinch-macos-") as temporary_name:
        app = stage_app(
            executable,
            Path(temporary_name) / APP_NAME,
            package_version,
            icon=icon,
        )
        identity = application_identity.strip() or "-"
        sign_command = [codesign, "--force", "--deep", "--options", "runtime"]
        if application_identity:
            sign_command.append("--timestamp")
        sign_command.extend(["--sign", identity, str(app)])
        _run(sign_command, "macOS application signing")
        _run(
            [codesign, "--verify", "--deep", "--strict", "--verbose=2", str(app)],
            "macOS application signature verification",
        )
        command = [
            pkgbuild,
            "--component",
            str(app),
            "--install-location",
            "/Applications",
            "--identifier",
            BUNDLE_IDENTIFIER,
            "--version",
            package_version,
        ]
        if installer_identity:
            command.extend(["--sign", installer_identity.strip()])
        command.append(str(temporary_output))
        try:
            _run(command, "macOS package creation")
            _regular_file(temporary_output, "Built macOS installer")
            os.replace(temporary_output, output)
        finally:
            temporary_output.unlink(missing_ok=True)

    if all(notary_values):
        key = _regular_file(notary_key, "App Store Connect private key")  # type: ignore[arg-type]
        xcrun = _tool("xcrun")
        _run(
            [
                xcrun,
                "notarytool",
                "submit",
                str(output),
                "--key",
                str(key),
                "--key-id",
                notary_key_id.strip(),
                "--issuer",
                notary_issuer.strip(),
                "--wait",
            ],
            "macOS notarization",
        )
        _run([xcrun, "stapler", "staple", str(output)], "notarization stapling")
        _run([xcrun, "stapler", "validate", str(output)], "staple validation")
        spctl = _tool("spctl")
        _run(
            [spctl, "--assess", "--type", "install", "--verbose=2", str(output)],
            "Gatekeeper package assessment",
        )

    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_name(f"{output.name}.sha256").write_text(
        f"{digest}  {output.name}\n", encoding="ascii"
    )
    return output.resolve()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build, sign, and optionally notarize Dayfinch for macOS"
    )
    parser.add_argument("executable", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--icon", type=Path, default=DEFAULT_ICON)
    parser.add_argument("--application-identity", default="")
    parser.add_argument("--installer-identity", default="")
    parser.add_argument("--notary-key", type=Path)
    parser.add_argument("--notary-key-id", default="")
    parser.add_argument("--notary-issuer", default="")
    arguments = parser.parse_args()
    try:
        result = build_pkg(
            arguments.executable,
            arguments.output,
            arguments.version,
            icon=arguments.icon,
            application_identity=arguments.application_identity,
            installer_identity=arguments.installer_identity,
            notary_key=arguments.notary_key,
            notary_key_id=arguments.notary_key_id,
            notary_issuer=arguments.notary_issuer,
        )
    except MacOSPackageError as exc:
        raise SystemExit(str(exc)) from exc
    print(result)


if __name__ == "__main__":
    main()

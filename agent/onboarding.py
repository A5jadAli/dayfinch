from __future__ import annotations

import os
import stat
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .config import AgentConfig

MAX_CONFIGURATION_BYTES = 64 * 1024


class ConfigurationImportError(RuntimeError):
    pass


def default_config_path(
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    platform_name = platform or sys.platform
    environment = environ if environ is not None else os.environ
    home_directory = (home or Path.home()).expanduser()
    if platform_name == "win32":
        configured = environment.get("APPDATA", "").strip()
        root = (
            Path(configured) if configured else home_directory / "AppData" / "Roaming"
        )
        return root / "Dayfinch" / "agent.toml"
    if platform_name == "darwin":
        return (
            home_directory
            / "Library"
            / "Application Support"
            / "Dayfinch"
            / "agent.toml"
        )
    configured = environment.get("XDG_CONFIG_HOME", "").strip()
    root = (
        Path(configured)
        if configured and Path(configured).is_absolute()
        else home_directory / ".config"
    )
    return root / "dayfinch" / "agent.toml"


def _regular_source(path: Path) -> Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ConfigurationImportError(
            "The selected configuration is unavailable"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ConfigurationImportError(
            "The selected configuration must be a regular non-symlink file"
        )
    if not 0 < metadata.st_size <= MAX_CONFIGURATION_BYTES:
        raise ConfigurationImportError("The selected configuration has an invalid size")
    return path.resolve()


def install_configuration(
    source: Path,
    destination: Path,
    *,
    replace: bool = False,
) -> Path:
    selected = _regular_source(source.expanduser())
    target = destination.expanduser()
    if target.is_symlink():
        raise ConfigurationImportError(
            "The configuration destination cannot be a symlink"
        )
    try:
        if selected == target.resolve(strict=False):
            AgentConfig.from_file(selected)
            selected.chmod(0o600)
            return selected
    except OSError as exc:
        raise ConfigurationImportError(
            "The configuration destination is unavailable"
        ) from exc
    if target.exists() and not replace:
        raise ConfigurationImportError("A Dayfinch configuration is already installed")
    try:
        # Validate before copying so malformed or non-consented enrollment files
        # never replace a working local configuration.
        AgentConfig.from_file(selected)
    except (OSError, ValueError) as exc:
        raise ConfigurationImportError(
            "The selected Dayfinch configuration is invalid"
        ) from exc
    temporary: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.parent.is_symlink() or not target.parent.is_dir():
            raise ConfigurationImportError(
                "The configuration directory must be a private directory"
            )
        target.parent.chmod(0o700)
        with (
            selected.open("rb") as source_stream,
            tempfile.NamedTemporaryFile(
                dir=target.parent,
                prefix=".agent.toml.",
                suffix=".part",
                delete=False,
            ) as target_stream,
        ):
            temporary = Path(target_stream.name)
            payload = source_stream.read(MAX_CONFIGURATION_BYTES + 1)
            if not payload or len(payload) > MAX_CONFIGURATION_BYTES:
                raise ConfigurationImportError(
                    "The selected configuration has an invalid size"
                )
            target_stream.write(payload)
            target_stream.flush()
            os.fsync(target_stream.fileno())
        temporary.chmod(0o600)
        # Resolve relative queue paths against their installed location and
        # validate the exact bytes that are about to become authoritative.
        AgentConfig.from_file(temporary)
        os.replace(temporary, target)
        temporary = None
        target.chmod(0o600)
        if hasattr(os, "O_DIRECTORY"):
            descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except ConfigurationImportError:
        raise
    except (OSError, ValueError) as exc:
        raise ConfigurationImportError(
            "The Dayfinch configuration could not be installed safely"
        ) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target.resolve()


def prompt_for_configuration(destination: Path) -> Path:
    root = None
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox

        root = tk.Tk()
        root.withdraw()
        root.update_idletasks()
        messagebox.showinfo(
            "Connect Dayfinch Tracker",
            "Choose the agent.toml file downloaded after connecting this computer "
            "from your Dayfinch project. It contains a private device enrollment "
            "credential and must not be shared.",
            parent=root,
        )
        selected = filedialog.askopenfilename(
            parent=root,
            title="Choose Dayfinch agent.toml",
            filetypes=(("Dayfinch configuration", "*.toml"), ("All files", "*")),
        )
        if not selected:
            raise ConfigurationImportError("No Dayfinch configuration was selected")
        try:
            installed = install_configuration(Path(selected), destination)
        except ConfigurationImportError as exc:
            messagebox.showerror("Dayfinch configuration error", str(exc), parent=root)
            raise
        messagebox.showinfo(
            "Dayfinch connected",
            "This computer is connected. The tracker will now verify permissions "
            "and open in Not tracking mode.",
            parent=root,
        )
        return installed
    except ConfigurationImportError:
        raise
    except Exception as exc:
        raise ConfigurationImportError(
            "Dayfinch could not open the first-run configuration window"
        ) from exc
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                # The native window may already have been torn down by the OS.
                pass

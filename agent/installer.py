from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class InstallError(RuntimeError):
    """A staged update could not be installed without risking the current binary."""


def installed_in_macos_app_bundle(executable: Path) -> bool:
    """Return whether an executable is inside a conventional signed .app bundle."""
    parts = executable.absolute().parts
    for index, part in enumerate(parts):
        if not part.lower().endswith(".app"):
            continue
        tail = parts[index + 1 :]
        if len(tail) >= 3 and tail[0] == "Contents" and tail[1] == "MacOS":
            return True
    return False


def ensure_in_place_update_safe(executable: Path) -> None:
    if installed_in_macos_app_bundle(executable):
        raise InstallError(
            "A signed macOS app cannot be updated by replacing its inner executable; "
            "install the signed and notarized Dayfinch .pkg instead"
        )


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path, label: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise InstallError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise InstallError(f"{label} must be a regular non-symlink file")
    return path.resolve()


def _private_directory(path: Path) -> Path:
    if path.exists() and path.is_symlink():
        raise InstallError("Update state directory cannot be a symlink")
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    except OSError as exc:
        raise InstallError("Update state directory cannot be made private") from exc
    return path.resolve()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".part",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".part",
        )
        os.close(descriptor)
        temporary = Path(name)
        shutil.copyfile(source, temporary)
        temporary.chmod(0o700)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
        _fsync_directory(destination.parent)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def create_install_plan(
    current_executable: Path,
    staged_artifact: Path,
    state_directory: Path,
    current_version: str,
    new_version: str,
    *,
    original_pid: int | None = None,
) -> tuple[Path, Path]:
    ensure_in_place_update_safe(current_executable)
    current = _regular_file(current_executable, "Current executable")
    staged = _regular_file(staged_artifact, "Staged update")
    state = _private_directory(state_directory)
    try:
        staged.relative_to(state)
    except ValueError as exc:
        raise InstallError(
            "Staged update must be inside the private update directory"
        ) from exc
    if current == staged:
        raise InstallError("Staged update cannot overwrite itself")

    plan_id = str(uuid.uuid4())
    suffix = ".exe" if current.suffix.lower() == ".exe" else ""
    helper = state / "helpers" / f"dayfinch-updater-{plan_id}{suffix}"
    backup = state / "rollback" / f"{current.name}.{current_version}.{plan_id}.bak"
    plan_path = state / "plans" / f"{plan_id}.json"
    _atomic_copy(current, helper)
    payload = {
        "schema": 1,
        "id": plan_id,
        "status": "pending",
        "target": str(current),
        "staged": str(staged),
        "backup": str(backup),
        "old_sha256": _digest(current),
        "new_sha256": _digest(staged),
        "old_size": current.stat().st_size,
        "new_size": staged.stat().st_size,
        "version_from": current_version,
        "version_to": new_version,
        "original_pid": original_pid if original_pid is not None else os.getpid(),
        "created_at": datetime.now(UTC).isoformat(),
    }
    _write_json(plan_path, payload)
    return plan_path, helper


def _load_plan(plan_path: Path) -> tuple[dict[str, Any], Path, Path, Path]:
    plan_file = _regular_file(plan_path, "Update plan")
    try:
        payload = json.loads(plan_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError("Update plan is invalid") from exc
    required = {
        "schema",
        "id",
        "status",
        "target",
        "staged",
        "backup",
        "old_sha256",
        "new_sha256",
        "old_size",
        "new_size",
        "original_pid",
        "version_from",
        "version_to",
    }
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != 1
        or not required <= payload.keys()
    ):
        raise InstallError("Update plan fields are invalid")
    state = plan_file.parent.parent.resolve()
    target = Path(str(payload["target"]))
    staged = Path(str(payload["staged"]))
    backup = Path(str(payload["backup"]))
    if not target.is_absolute() or not staged.is_absolute() or not backup.is_absolute():
        raise InstallError("Update plan paths must be absolute")
    for confined, label in ((staged, "Staged update"), (backup, "Rollback copy")):
        try:
            confined.resolve().relative_to(state)
        except ValueError as exc:
            raise InstallError(f"{label} escapes the update directory") from exc
    if not all(
        isinstance(payload[key], str)
        for key in (
            "id",
            "status",
            "old_sha256",
            "new_sha256",
            "version_from",
            "version_to",
        )
    ):
        raise InstallError("Update plan fields are invalid")
    if not all(
        isinstance(payload[key], int)
        for key in ("old_size", "new_size", "original_pid")
    ):
        raise InstallError("Update plan numeric fields are invalid")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", payload["old_sha256"])
        or not re.fullmatch(r"[0-9a-f]{64}", payload["new_sha256"])
        or payload["old_size"] < 1
        or payload["new_size"] < 1
        or payload["original_pid"] < 0
        or len({target.resolve(), staged.resolve(), backup.resolve()}) != 3
    ):
        raise InstallError("Update plan integrity fields are invalid")
    return payload, target, staged, backup


def _matches(path: Path, size: int, digest: str) -> bool:
    return path.is_file() and path.stat().st_size == size and _digest(path) == digest


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_exit(pid: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while _pid_alive(pid):
        if time.monotonic() >= deadline:
            raise InstallError("Timed out waiting for the running agent to exit")
        time.sleep(0.1)


def apply_install_plan(
    plan_path: Path,
    *,
    wait_timeout: float = 60.0,
    health_timeout: float = 60.0,
) -> dict[str, Any]:
    payload, target, staged, backup = _load_plan(plan_path)
    if payload["status"] != "pending":
        raise InstallError("Update plan has already been processed")
    _wait_for_exit(payload["original_pid"], wait_timeout)
    target = _regular_file(target, "Installed executable")
    staged = _regular_file(staged, "Staged update")
    if not _matches(target, payload["old_size"], payload["old_sha256"]):
        raise InstallError("Installed executable changed after the update was staged")
    if not _matches(staged, payload["new_size"], payload["new_sha256"]):
        raise InstallError("Staged update no longer matches its verified digest")

    payload["status"] = "replacing"
    payload["updated_at"] = datetime.now(UTC).isoformat()
    _write_json(plan_path, payload)
    try:
        _atomic_copy(target, backup)
        if not _matches(backup, payload["old_size"], payload["old_sha256"]):
            raise InstallError("Rollback copy could not be verified")
    except Exception as exc:
        payload["status"] = "backup_failed"
        payload["error"] = type(exc).__name__
        _write_json(plan_path, payload)
        raise InstallError("Could not create a verified rollback copy") from exc
    try:
        _atomic_copy(staged, target)
        if not _matches(target, payload["new_size"], payload["new_sha256"]):
            raise InstallError("Installed update could not be verified")
        completed = subprocess.run(
            [str(target), "--diagnose"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=health_timeout,
            check=False,
        )
        if completed.returncode != 0:
            raise InstallError(
                f"Updated agent diagnostics exited with code {completed.returncode}"
            )
        if not _matches(target, payload["new_size"], payload["new_sha256"]):
            raise InstallError("Updated executable changed during diagnostics")
        payload["status"] = "installed"
        payload["updated_at"] = datetime.now(UTC).isoformat()
        _write_json(plan_path, payload)
    except Exception as exc:
        try:
            _atomic_copy(backup, target)
            restored = _matches(target, payload["old_size"], payload["old_sha256"])
        except Exception as rollback_exc:
            payload["status"] = "recovery_required"
            payload["error"] = type(exc).__name__
            payload["rollback_error"] = type(rollback_exc).__name__
            _write_json(plan_path, payload)
            raise InstallError(
                "Update failed and automatic rollback also failed"
            ) from rollback_exc
        payload["status"] = "rolled_back" if restored else "recovery_required"
        payload["error"] = type(exc).__name__
        payload["updated_at"] = datetime.now(UTC).isoformat()
        _write_json(plan_path, payload)
        if not restored:
            raise InstallError(
                "Update failed and rollback could not be verified"
            ) from exc
        raise InstallError(
            "Updated agent failed diagnostics and was rolled back"
        ) from exc

    try:
        staged.unlink(missing_ok=True)
    except OSError:
        pass
    return payload


def reconcile_install_plans(state_directory: Path, current_executable: Path) -> int:
    """Resolve power-loss states after the current binary has passed diagnostics."""
    state = state_directory.resolve()
    plans = state / "plans"
    if not plans.is_dir():
        return 0
    current = _regular_file(current_executable, "Installed executable")
    resolved = 0
    for plan_path in sorted(plans.glob("*.json"), reverse=True)[:100]:
        payload, target, staged, _backup = _load_plan(plan_path)
        if target.resolve() != current:
            continue
        if payload["status"] not in {"replacing", "recovery_required", "backup_failed"}:
            continue
        if _matches(current, payload["new_size"], payload["new_sha256"]):
            payload["status"] = "installed"
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass
        elif _matches(current, payload["old_size"], payload["old_sha256"]):
            payload["status"] = "rolled_back"
        else:
            raise InstallError(
                f"Update recovery requires operator action; plan: {plan_path}"
            )
        payload["reconciled_at"] = datetime.now(UTC).isoformat()
        _write_json(plan_path, payload)
        resolved += 1
    return resolved


def launch_install_helper(
    current_executable: Path,
    staged_artifact: Path,
    state_directory: Path,
    current_version: str,
    new_version: str,
) -> Path:
    plan_path, helper = create_install_plan(
        current_executable,
        staged_artifact,
        state_directory,
        current_version,
        new_version,
    )
    arguments: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        arguments["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        arguments["start_new_session"] = True
    try:
        subprocess.Popen(
            [str(helper), "--apply-update-plan", str(plan_path)], **arguments
        )
    except OSError as exc:
        payload, _, _, _ = _load_plan(plan_path)
        payload["status"] = "launch_failed"
        payload["error"] = type(exc).__name__
        _write_json(plan_path, payload)
        raise InstallError("Could not launch the detached update helper") from exc
    return plan_path

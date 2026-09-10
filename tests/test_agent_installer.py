from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.installer import (
    InstallError,
    apply_install_plan,
    create_install_plan,
    ensure_in_place_update_safe,
    installed_in_macos_app_bundle,
    launch_install_helper,
    reconcile_install_plans,
)


def _executable(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o700)
    return path


def test_signed_macos_app_bundle_requires_package_level_update():
    bundled = Path("/Applications/Dayfinch Tracker.app/Contents/MacOS/Dayfinch-Agent")

    assert installed_in_macos_app_bundle(bundled) is True
    assert installed_in_macos_app_bundle(Path("/opt/dayfinch/dayfinch-agent")) is False
    with pytest.raises(InstallError, match="signed and notarized Dayfinch .pkg"):
        ensure_in_place_update_safe(bundled)


def test_install_replaces_only_verified_target_and_keeps_rollback(
    tmp_path, monkeypatch
):
    target = _executable(tmp_path / "bin" / "dayfinch-agent", b"old verified binary")
    state = tmp_path / "updates"
    staged = _executable(state / "dayfinch-agent-new", b"new verified binary")
    plan, helper = create_install_plan(
        target, staged, state, "1.0.0", "1.1.0", original_pid=0
    )
    calls = []

    def healthy(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("agent.installer.subprocess.run", healthy)
    result = apply_install_plan(plan)

    assert result["status"] == "installed"
    assert target.read_bytes() == b"new verified binary"
    assert Path(result["backup"]).read_bytes() == b"old verified binary"
    assert not staged.exists()
    assert helper.read_bytes() == b"old verified binary"
    assert calls[0][0] == [str(target.resolve()), "--diagnose"]
    assert json.loads(plan.read_text())["status"] == "installed"


def test_failed_new_binary_diagnostics_roll_back_atomically(tmp_path, monkeypatch):
    target = _executable(tmp_path / "bin" / "dayfinch-agent", b"known good binary")
    state = tmp_path / "updates"
    staged = _executable(state / "dayfinch-agent-new", b"broken binary")
    plan, _ = create_install_plan(
        target, staged, state, "1.0.0", "1.1.0", original_pid=0
    )
    monkeypatch.setattr(
        "agent.installer.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=9),
    )

    with pytest.raises(InstallError, match="rolled back"):
        apply_install_plan(plan)

    assert target.read_bytes() == b"known good binary"
    assert staged.read_bytes() == b"broken binary"
    assert json.loads(plan.read_text())["status"] == "rolled_back"


def test_changed_target_or_staged_artifact_is_never_installed(tmp_path, monkeypatch):
    target = _executable(tmp_path / "bin" / "dayfinch-agent", b"old binary")
    state = tmp_path / "updates"
    staged = _executable(state / "dayfinch-agent-new", b"new binary")
    plan, _ = create_install_plan(
        target, staged, state, "1.0.0", "1.1.0", original_pid=0
    )
    target.write_bytes(b"changed by another installer")
    monkeypatch.setattr(
        "agent.installer.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("diagnostics must not run"),
    )

    with pytest.raises(InstallError, match="changed after"):
        apply_install_plan(plan)

    assert target.read_bytes() == b"changed by another installer"
    assert not Path(json.loads(plan.read_text())["backup"]).exists()


def test_plan_confines_staged_and_rollback_files_to_private_state(tmp_path):
    target = _executable(tmp_path / "bin" / "dayfinch-agent", b"old")
    outside = _executable(tmp_path / "download", b"new")

    with pytest.raises(InstallError, match="inside the private update directory"):
        create_install_plan(target, outside, tmp_path / "updates", "1.0.0", "1.1.0")


def test_detached_helper_receives_plan_and_launch_failure_is_durable(
    tmp_path, monkeypatch
):
    target = _executable(tmp_path / "bin" / "dayfinch-agent", b"old")
    state = tmp_path / "updates"
    staged = _executable(state / "dayfinch-agent-new", b"new")
    launched = []

    def fake_popen(command, **kwargs):
        launched.append((command, kwargs))
        return SimpleNamespace(pid=123)

    monkeypatch.setattr("agent.installer.subprocess.Popen", fake_popen)
    plan = launch_install_helper(target, staged, state, "1.0.0", "1.1.0")
    assert launched[0][0][1:] == ["--apply-update-plan", str(plan)]
    assert json.loads(plan.read_text())["status"] == "pending"

    second = _executable(state / "dayfinch-agent-second", b"newer")
    monkeypatch.setattr(
        "agent.installer.subprocess.Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("blocked")),
    )
    with pytest.raises(InstallError, match="launch"):
        launch_install_helper(target, second, state, "1.0.0", "1.2.0")
    failed_plans = [
        json.loads(path.read_text())
        for path in (state / "plans").glob("*.json")
        if path != plan
    ]
    assert failed_plans[0]["status"] == "launch_failed"


def test_startup_reconciles_power_loss_from_verified_current_binary(tmp_path):
    target = _executable(tmp_path / "bin" / "dayfinch-agent", b"old")
    state = tmp_path / "updates"
    staged = _executable(state / "dayfinch-agent-new", b"new")
    plan, _ = create_install_plan(
        target, staged, state, "1.0.0", "1.1.0", original_pid=0
    )
    payload = json.loads(plan.read_text())
    payload["status"] = "replacing"
    plan.write_text(json.dumps(payload))
    target.write_bytes(b"new")

    assert reconcile_install_plans(state, target) == 1
    assert json.loads(plan.read_text())["status"] == "installed"
    assert not staged.exists()


def test_startup_refuses_ambiguous_interrupted_replacement(tmp_path):
    target = _executable(tmp_path / "bin" / "dayfinch-agent", b"old")
    state = tmp_path / "updates"
    staged = _executable(state / "dayfinch-agent-new", b"new")
    plan, _ = create_install_plan(
        target, staged, state, "1.0.0", "1.1.0", original_pid=0
    )
    payload = json.loads(plan.read_text())
    payload["status"] = "recovery_required"
    plan.write_text(json.dumps(payload))
    target.write_bytes(b"unknown third binary")

    with pytest.raises(InstallError, match="operator action"):
        reconcile_install_plans(state, target)

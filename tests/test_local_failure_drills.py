import subprocess
from pathlib import Path

import pytest

from scripts.local_failure_drills import (
    ComposeControl,
    DrillError,
    parser,
    validate_local_target,
)


def test_failure_drills_are_restricted_to_loopback():
    assert validate_local_target("http://127.0.0.1:8000/") == (
        "http://127.0.0.1:8000"
    )
    with pytest.raises(DrillError, match="loopback"):
        validate_local_target("https://tracker.example.test")
    with pytest.raises(DrillError, match="loopback"):
        validate_local_target("http://localhost:8000/unrelated")


def test_compose_control_uses_only_the_local_override(tmp_path: Path, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    control = ComposeControl(tmp_path)
    control.stop("minio")
    control.start("minio")

    assert calls[0][0] == [
        "docker",
        "compose",
        "-f",
        "compose.yaml",
        "-f",
        "compose.local.yaml",
        "stop",
        "--timeout",
        "10",
        "minio",
    ]
    assert calls[1][0][-3:] == ["up", "-d", "minio"]
    assert all(call[1]["cwd"] == tmp_path for call in calls)


def test_failure_drill_requires_explicit_disruption_acknowledgement():
    arguments = parser().parse_args([])
    assert not arguments.acknowledge_local_disruption
    assert arguments.admin_password_env == "TRACKER_ADMIN_PASSWORD"

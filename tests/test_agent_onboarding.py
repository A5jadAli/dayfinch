from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from agent.config import AgentConfig
from agent.onboarding import (
    ConfigurationImportError,
    default_config_path,
    install_configuration,
)


def _config(path: Path, *, token: str = "x" * 40) -> None:
    path.write_text(
        "\n".join(
            (
                'server_url = "https://tracker.example.test"',
                f'device_token = "{token}"',
                "consent_confirmed = true",
                'queue_dir = "runtime/queue"',
            )
        ),
        encoding="utf-8",
    )


def test_platform_default_configuration_paths_are_per_user(tmp_path):
    assert (
        default_config_path(platform="linux", environ={}, home=tmp_path)
        == tmp_path / ".config/dayfinch/agent.toml"
    )
    assert (
        default_config_path(
            platform="linux",
            environ={"XDG_CONFIG_HOME": str(tmp_path / "xdg")},
            home=tmp_path,
        )
        == tmp_path / "xdg/dayfinch/agent.toml"
    )
    assert (
        default_config_path(
            platform="linux", environ={"XDG_CONFIG_HOME": "relative"}, home=tmp_path
        )
        == tmp_path / ".config/dayfinch/agent.toml"
    )
    assert (
        default_config_path(platform="darwin", environ={}, home=tmp_path)
        == tmp_path / "Library/Application Support/Dayfinch/agent.toml"
    )
    assert default_config_path(
        platform="win32",
        environ={"APPDATA": "C:/Users/Dev/AppData/Roaming"},
        home=tmp_path,
    ) == Path("C:/Users/Dev/AppData/Roaming/Dayfinch/agent.toml")


def test_configuration_import_is_atomic_private_and_rebased(tmp_path):
    source = tmp_path / "downloaded-agent.toml"
    destination = tmp_path / "private/dayfinch/agent.toml"
    _config(source)

    installed = install_configuration(source, destination)

    assert installed == destination.resolve()
    assert destination.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert destination.stat().st_mode & 0o777 == 0o600
    assert destination.parent.stat().st_mode & 0o777 == 0o700
    parsed = AgentConfig.from_file(destination)
    assert parsed.queue_dir == destination.parent / "runtime/queue"
    assert not list(destination.parent.glob("*.part"))


def test_configuration_import_fails_closed_and_explicit_replace_is_atomic(tmp_path):
    source = tmp_path / "agent.toml"
    destination = tmp_path / "installed/agent.toml"
    _config(source)
    install_configuration(source, destination)

    replacement = tmp_path / "replacement.toml"
    _config(replacement, token="y" * 40)
    with pytest.raises(ConfigurationImportError, match="already installed"):
        install_configuration(replacement, destination)
    assert 'device_token = "xxxxxxxx' in destination.read_text(encoding="utf-8")

    install_configuration(replacement, destination, replace=True)
    assert 'device_token = "yyyyyyyy' in destination.read_text(encoding="utf-8")
    assert destination.stat().st_mode & 0o777 == 0o600

    invalid = tmp_path / "invalid.toml"
    invalid.write_text('device_token = "secret"', encoding="utf-8")
    with pytest.raises(ConfigurationImportError, match="invalid"):
        install_configuration(invalid, destination, replace=True)
    assert 'device_token = "yyyyyyyy' in destination.read_text(encoding="utf-8")


@pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(os, "symlink"),
    reason="unprivileged symlink creation is not portable on Windows",
)
def test_configuration_import_rejects_symlink_sources_and_destinations(tmp_path):
    source = tmp_path / "agent.toml"
    _config(source)
    source_link = tmp_path / "source-link.toml"
    source_link.symlink_to(source)
    with pytest.raises(ConfigurationImportError, match="non-symlink"):
        install_configuration(source_link, tmp_path / "target/agent.toml")

    destination_target = tmp_path / "destination-target.toml"
    destination_target.write_text("do not replace", encoding="utf-8")
    destination_link = tmp_path / "destination-link.toml"
    destination_link.symlink_to(destination_target)
    with pytest.raises(ConfigurationImportError, match="cannot be a symlink"):
        install_configuration(source, destination_link, replace=True)
    assert destination_target.read_text(encoding="utf-8") == "do not replace"


def test_configuration_import_rejects_oversized_files(tmp_path):
    source = tmp_path / "oversized.toml"
    source.write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(ConfigurationImportError, match="size"):
        install_configuration(source, tmp_path / "target/agent.toml")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_same_installed_configuration_is_accepted_and_hardened(tmp_path):
    source = tmp_path / "agent.toml"
    _config(source)
    source.chmod(0o644)
    assert install_configuration(source, source) == source.resolve()
    assert source.stat().st_mode & 0o777 == 0o600

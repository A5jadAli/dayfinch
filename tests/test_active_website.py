"""Foreground website domain capture — host only, never the full URL."""

from __future__ import annotations

import subprocess

import pytest

from agent import active_app


def test_non_browser_apps_report_no_website():
    assert active_app.active_website("Slack") == ""
    assert active_app.active_website("Code") == ""


def test_host_extraction_strips_path_query_and_www():
    assert active_app._host("https://www.github.com/foo/bar?x=1") == "github.com"
    assert (
        active_app._host("https://mail.google.com/mail/u/0/#inbox") == "mail.google.com"
    )


def test_non_http_schemes_are_rejected():
    assert active_app._host("file:///etc/passwd") == ""
    assert active_app._host("chrome://settings") == ""
    assert active_app._host("") == ""


def test_macos_browser_url_is_reduced_to_host(monkeypatch):
    monkeypatch.setattr(active_app.platform, "system", lambda: "Darwin")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="https://github.com/anthropics/repo/pull/9?tab=files\n",
            stderr="",
        )

    monkeypatch.setattr(active_app.subprocess, "run", fake_run)

    assert active_app.active_website("Google Chrome") == "github.com"


def test_website_capture_swallows_scripting_errors(monkeypatch):
    monkeypatch.setattr(active_app.platform, "system", lambda: "Darwin")

    def boom(cmd, **kwargs):
        raise subprocess.SubprocessError("automation permission denied")

    monkeypatch.setattr(active_app.subprocess, "run", boom)

    assert active_app.active_website("Safari") == ""


@pytest.mark.parametrize("system", ["Linux", "Windows"])
def test_url_capture_unavailable_platforms_return_empty(monkeypatch, system):
    monkeypatch.setattr(active_app.platform, "system", lambda: system)

    assert active_app.active_website("Google Chrome") == ""

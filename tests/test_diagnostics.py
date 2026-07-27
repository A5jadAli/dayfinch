from agent import diagnostics
from agent.diagnostics import DiagnosticCheck


def test_failure_summary_and_formatting():
    checks = [
        DiagnosticCheck("capture", "pass", "available"),
        DiagnosticCheck("portal", "fail", "missing"),
    ]

    assert diagnostics.has_failures(checks)
    assert "[PASS] capture: available" in diagnostics.format_diagnostics(checks)
    assert "[FAIL] portal: missing" in diagnostics.format_diagnostics(checks)


def test_wayland_diagnostics_are_transparent_about_input_limits(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setattr(diagnostics, "_portal_available", lambda: True)
    monkeypatch.setattr(
        diagnostics,
        "_dependency_check",
        lambda _module, name: DiagnosticCheck(name, "pass", "available"),
    )

    checks = diagnostics._linux_checks()
    by_name = {check.name: check for check in checks}

    assert by_name["display-session"].status == "pass"
    assert by_name["screenshot-portal"].status == "pass"
    assert by_name["aggregate-input"].status == "warn"
    assert "blocks passive" in by_name["aggregate-input"].message

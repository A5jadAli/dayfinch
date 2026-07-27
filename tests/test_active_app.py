from agent import active_app


def test_wayland_reports_desktop_without_inspecting_window_titles(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME:GNOME-Classic")

    assert active_app._linux_app() == "GNOME desktop"

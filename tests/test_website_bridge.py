from __future__ import annotations

import time
from http.client import HTTPConnection

from agent.website_bridge import WebsiteBridge, normalize_domain


def test_domain_normalization_never_retains_page_details():
    assert (
        normalize_domain("https://www.github.com/org/repo?q=secret#part")
        == "github.com"
    )
    assert normalize_domain("mail.google.com") == "mail.google.com"
    assert normalize_domain("file:///private/file") == ""


def test_bridge_expires_reports_instead_of_reusing_background_tab():
    bridge = WebsiteBridge("t" * 40, max_age_seconds=0.01)
    bridge._update(normalize_domain("https://www.example.com/private?q=1"))

    assert bridge.current_domain() == "example.com"
    time.sleep(0.02)
    assert bridge.current_domain() == ""


class _TimerController:
    def __init__(self):
        self.actions = []

    def browser_timer_snapshot(self):
        return {
            "state": "stopped",
            "status": "Not tracking",
            "project_id": "project-1",
            "task_id": "",
            "projects": [{"id": "project-1", "name": "Project", "tasks": []}],
        }

    def browser_timer_action(self, action, project_id, task_id, note):
        self.actions.append((action, project_id, task_id, note))
        return {**self.browser_timer_snapshot(), "state": "active"}


def _request(bridge, method, path, token, body=""):
    connection = HTTPConnection("127.0.0.1", bridge.port, timeout=2)
    headers = {
        "Origin": "chrome-extension://dayfinch-test",
        "Authorization": f"Bearer {token}",
    }
    if body:
        headers["Content-Type"] = "application/json"
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    return response.status, payload, response.headers


def test_extension_timer_bridge_requires_token_and_controls_visible_agent():
    token = "b" * 40
    controller = _TimerController()
    bridge = WebsiteBridge(token, port=0)
    bridge.set_timer_controller(controller)
    assert bridge.start()
    try:
        denied, _, _ = _request(bridge, "GET", "/v1/timer", "wrong-token")
        assert denied == 401
        status, payload, headers = _request(bridge, "GET", "/v1/timer", token)
        assert status == 200
        assert b'"state":"stopped"' in payload
        assert headers["Cache-Control"] == "no-store"

        status, payload, _ = _request(
            bridge,
            "POST",
            "/v1/timer",
            token,
            '{"action":"start","project_id":"project-1","task_id":"","note":"Review"}',
        )
        assert status == 200
        assert b'"state":"active"' in payload
        assert controller.actions == [("start", "project-1", "", "Review")]
    finally:
        bridge.stop()


def test_extension_timer_bridge_rejects_non_extension_origins():
    bridge = WebsiteBridge("b" * 40, port=0)
    bridge.set_timer_controller(_TimerController())
    assert bridge.start()
    try:
        connection = HTTPConnection("127.0.0.1", bridge.port, timeout=2)
        connection.request(
            "GET",
            "/v1/timer",
            headers={
                "Origin": "https://evil.example",
                "Authorization": f"Bearer {'b' * 40}",
            },
        )
        response = connection.getresponse()
        response.read()
        connection.close()
        assert response.status == 404
    finally:
        bridge.stop()

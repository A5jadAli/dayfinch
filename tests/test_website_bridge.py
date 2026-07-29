from __future__ import annotations

import time

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

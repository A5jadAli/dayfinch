from __future__ import annotations

import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit


def normalize_domain(value: str) -> str:
    """Return a hostname only; paths, credentials, fragments, and queries vanish.

    Deliberately duplicated in api/services/privacy.py: this agent ships separately
    and must not import from the server package. Keep the two copies identical, or
    the browser extension and the server will disagree about what counts as a domain.
    """
    candidate = value.strip().lower()
    if not candidate:
        return ""
    parsed = urlsplit(candidate if "://" in candidate else f"https://{candidate}")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.rstrip(".").removeprefix("www.")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    if len(host) > 253 or any(not label for label in host.split(".")):
        return ""
    return host


class WebsiteBridge:
    """Receives active-tab domains from a consented local browser extension."""

    def __init__(
        self,
        token: str,
        port: int = 8765,
        *,
        # Chrome clamps alarms to one minute in packed extensions, so reports can be
        # 60s apart. Do not lower this below ~90s or a focused tab will read stale.
        max_age_seconds: float = 90.0,
    ) -> None:
        self.token = token
        self.port = port
        self.max_age_seconds = max_age_seconds
        self._lock = threading.Lock()
        self._domain = ""
        self._received_at = 0.0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._timer_controller: Any | None = None

    def set_timer_controller(self, controller: Any) -> None:
        self._timer_controller = controller

    def start(self) -> bool:
        if not self.token:
            return False
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/v1/timer" or not self._extension_origin():
                    self.send_error(404)
                    return
                if not self._authorized_header() or not bridge._timer_controller:
                    self.send_error(401)
                    return
                self._json_response(bridge._timer_controller.browser_timer_snapshot())

            def do_OPTIONS(self) -> None:  # noqa: N802
                if not self._extension_origin():
                    self.send_error(403)
                    return
                self.send_response(204)
                self._cors_headers()
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                if not self._extension_origin():
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 1 or length > 4096:
                        raise ValueError
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise ValueError
                    supplied = str(body.get("token", ""))
                    if not (
                        self._authorized_header()
                        or hmac.compare_digest(supplied, bridge.token)
                    ):
                        self.send_error(401)
                        return
                    if self.path == "/v1/active-domain":
                        domain = normalize_domain(str(body.get("domain", "")))
                        bridge._update(domain)
                    elif self.path == "/v1/timer" and bridge._timer_controller:
                        result = bridge._timer_controller.browser_timer_action(
                            str(body.get("action", "")),
                            str(body.get("project_id", "")),
                            str(body.get("task_id", "")),
                            str(body.get("note", "")),
                        )
                        self._json_response(result)
                        return
                    else:
                        self.send_error(404)
                        return
                except (ValueError, TypeError, json.JSONDecodeError):
                    self.send_error(422)
                    return
                self.send_response(204)
                self._cors_headers()
                self.end_headers()

            def _extension_origin(self) -> bool:
                origin = self.headers.get("Origin", "")
                return origin.startswith(("chrome-extension://", "moz-extension://"))

            def _authorized_header(self) -> bool:
                prefix = "Bearer "
                header = self.headers.get("Authorization", "")
                return header.startswith(prefix) and hmac.compare_digest(
                    header[len(prefix) :], bridge.token
                )

            def _json_response(self, payload: dict) -> None:
                encoded = json.dumps(payload, separators=(",", ":")).encode()
                self.send_response(200)
                self._cors_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(encoded)

            def _cors_headers(self) -> None:
                self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
                self.send_header(
                    "Access-Control-Allow-Headers", "Authorization, Content-Type"
                )
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Vary", "Origin")

            def log_message(self, _format: str, *_args: object) -> None:
                return

        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        except OSError:
            return False
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="website-bridge",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def current_domain(self) -> str:
        with self._lock:
            if time.monotonic() - self._received_at > self.max_age_seconds:
                return ""
            return self._domain

    def _update(self, domain: str) -> None:
        with self._lock:
            self._domain = domain
            self._received_at = time.monotonic()

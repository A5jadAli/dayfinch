from __future__ import annotations

import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


def normalize_domain(value: str) -> str:
    """Return a hostname only; paths, credentials, fragments, and queries vanish."""
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
        self, token: str, port: int = 8765, *, max_age_seconds: float = 90.0
    ) -> None:
        self.token = token
        self.port = port
        self.max_age_seconds = max_age_seconds
        self._lock = threading.Lock()
        self._domain = ""
        self._received_at = 0.0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if not self.token:
            return False
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_OPTIONS(self) -> None:  # noqa: N802
                if not self._extension_origin():
                    self.send_error(403)
                    return
                self.send_response(204)
                self._cors_headers()
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/v1/active-domain" or not self._extension_origin():
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 1 or length > 4096:
                        raise ValueError
                    body = json.loads(self.rfile.read(length))
                    supplied = str(body.get("token", ""))
                    if not hmac.compare_digest(supplied, bridge.token):
                        self.send_error(401)
                        return
                    domain = normalize_domain(str(body.get("domain", "")))
                    bridge._update(domain)
                except (ValueError, TypeError, json.JSONDecodeError):
                    self.send_error(400)
                    return
                self.send_response(204)
                self._cors_headers()
                self.end_headers()

            def _extension_origin(self) -> bool:
                origin = self.headers.get("Origin", "")
                return origin.startswith(("chrome-extension://", "moz-extension://"))

            def _cors_headers(self) -> None:
                self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                self.send_header("Vary", "Origin")

            def log_message(self, _format: str, *_args: object) -> None:
                return

        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        except OSError:
            return False
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

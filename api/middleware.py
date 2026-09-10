from __future__ import annotations

import asyncio
import hashlib
import hmac

from starlette.responses import JSONResponse


class RequestRateLimitMiddleware:
    """Replica-consistent request limits without retaining raw identities."""

    EXEMPT_PATHS = {"/health", "/livez", "/readyz", "/metrics"}
    REPLAY_PATHS = {
        "/api/v1/activity",
        "/api/v1/heartbeat",
        "/api/v1/location",
        "/api/v1/usage",
    }

    def __init__(self, app, *, database, settings, secret: str):
        self.app = app
        self.database = database
        self.settings = settings
        self.secret = secret.encode("utf-8")

    def _digest(self, value: str) -> str:
        return hmac.new(self.secret, value.encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def _authorization(scope) -> str:
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                return value.decode("latin-1")
        return ""

    def _bucket(self, scope) -> tuple[str, int]:
        path = scope.get("path", "")
        authorization = self._authorization(scope)
        scheme, _, token = authorization.partition(" ")
        if path.startswith("/api/v1/") and scheme.lower() == "bearer" and token:
            replay = path in self.REPLAY_PATHS
            category = "device-replay" if replay else "device"
            limit = (
                self.settings.device_replay_request_limit
                if replay
                else self.settings.device_request_limit
            )
            return self._digest(f"{category}:{token}"), limit
        user_id = str(scope.get("session", {}).get("user_id", ""))
        if user_id:
            return self._digest(f"web:{user_id}"), self.settings.web_request_limit
        client = scope.get("client")
        source = str(client[0]) if client else "unknown"
        return self._digest(f"anonymous:{source}"), self.settings.anonymous_request_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if (
            path in self.EXEMPT_PATHS
            or path == "/service-worker.js"
            or path.startswith("/static/")
        ):
            await self.app(scope, receive, send)
            return
        bucket, limit = self._bucket(scope)
        allowed, retry_after = await asyncio.to_thread(
            self.database.consume_request_limit,
            bucket,
            limit=limit,
            window_seconds=self.settings.rate_limit_window_seconds,
        )
        if not allowed:
            response = JSONResponse(
                {"detail": "Too many requests"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    """Add browser hardening and prevent sensitive dashboard HTML from caching."""

    def __init__(self, app, *, hsts: bool = False):
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                names = {name.lower() for name, _ in headers}

                def add(name: str, value: str) -> None:
                    encoded = name.lower().encode("latin-1")
                    if encoded not in names:
                        headers.append((encoded, value.encode("latin-1")))

                add("X-Content-Type-Options", "nosniff")
                add("X-Frame-Options", "DENY")
                add("Referrer-Policy", "no-referrer")
                add(
                    "Permissions-Policy",
                    "camera=(), microphone=(), geolocation=(self), payment=()",
                )
                add(
                    "Content-Security-Policy",
                    "; ".join(
                        (
                            "default-src 'self'",
                            "base-uri 'self'",
                            "connect-src 'self'",
                            "font-src 'self' data:",
                            "form-action 'self'",
                            "frame-ancestors 'none'",
                            "img-src 'self' data: blob:",
                            "object-src 'none'",
                            "script-src 'self' 'unsafe-inline'",
                            "style-src 'self' 'unsafe-inline'",
                            "worker-src 'self' blob:",
                        )
                    ),
                )
                if self.hsts:
                    add(
                        "Strict-Transport-Security",
                        "max-age=31536000; includeSubDomains",
                    )
                content_type = next(
                    (
                        value.lower()
                        for name, value in headers
                        if name.lower() == b"content-type"
                    ),
                    b"",
                )
                if b"text/html" in content_type:
                    add("Cache-Control", "private, no-store")
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)

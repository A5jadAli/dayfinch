from __future__ import annotations


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

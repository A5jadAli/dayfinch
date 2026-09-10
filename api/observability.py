from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
RESERVED_LOG_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__)


class JsonFormatter(logging.Formatter):
    """One-line structured logs without request bodies, query strings, or users."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in RESERVED_LOG_RECORD_FIELDS or key.startswith("_"):
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _metric_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_:]", "_", value)


def _label(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class MetricsRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = (
            defaultdict(float)
        )
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = (
            defaultdict(float)
        )

    @staticmethod
    def _key(
        name: str, labels: dict[str, object]
    ) -> tuple[str, tuple[tuple[str, str], ...]]:
        return _metric_name(name), tuple(
            sorted((key, str(value)) for key, value in labels.items())
        )

    def increment(self, name: str, value: float = 1, **labels: object) -> None:
        with self._lock:
            self._counters[self._key(name, labels)] += value

    def gauge(self, name: str, delta: float, **labels: object) -> None:
        with self._lock:
            key = self._key(name, labels)
            self._gauges[key] = max(0, self._gauges[key] + delta)

    def set_gauge(self, name: str, value: float, **labels: object) -> None:
        with self._lock:
            self._gauges[self._key(name, labels)] = value

    def render(self) -> str:
        with self._lock:
            samples = [*self._counters.items(), *self._gauges.items()]
        lines = []
        for (name, labels), value in sorted(samples):
            rendered_labels = ",".join(
                f'{_metric_name(key)}="{_label(label)}"' for key, label in labels
            )
            suffix = f"{{{rendered_labels}}}" if rendered_labels else ""
            lines.append(f"{name}{suffix} {value:g}")
        return "\n".join(lines) + "\n"


class ObservabilityMiddleware:
    def __init__(self, app, *, metrics: MetricsRegistry):
        self.app = app
        self.metrics = metrics
        self.logger = logging.getLogger("dayfinch.request")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied_request_id = next(
            (
                value.decode("latin-1")
                for name, value in scope.get("headers", [])
                if name.lower() == b"x-request-id"
            ),
            "",
        )
        request_id = (
            supplied_request_id
            if REQUEST_ID_PATTERN.fullmatch(supplied_request_id)
            else str(uuid4())
        )
        scope["dayfinch.request_id"] = request_id
        method = scope.get("method", "UNKNOWN")
        status_code = 500
        started = time.perf_counter()
        self.metrics.gauge("dayfinch_http_requests_in_flight", 1)

        async def send_observed(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_observed)
        except Exception as exc:
            route = self._route(scope)
            self._record(method, route, 500, started, request_id)
            self.logger.error(
                "http_request_failed",
                extra={
                    "request_id": request_id,
                    "method": method,
                    "route": route,
                    "status": 500,
                    "exception_type": type(exc).__name__,
                },
            )
            raise
        else:
            self._record(method, self._route(scope), status_code, started, request_id)
        finally:
            self.metrics.gauge("dayfinch_http_requests_in_flight", -1)

    @staticmethod
    def _route(scope) -> str:
        route = scope.get("route")
        return getattr(route, "path", "unmatched")

    def _record(
        self,
        method: str,
        route: str,
        status_code: int,
        started: float,
        request_id: str,
    ) -> None:
        duration = time.perf_counter() - started
        labels = {"method": method, "route": route, "status": status_code}
        self.metrics.increment("dayfinch_http_requests_total", **labels)
        self.metrics.increment(
            "dayfinch_http_request_duration_seconds_sum", duration, **labels
        )
        self.metrics.increment("dayfinch_http_request_duration_seconds_count", **labels)
        self.logger.info(
            "http_request_completed",
            extra={
                "request_id": request_id,
                "method": method,
                "route": route,
                "status": status_code,
                "duration_ms": round(duration * 1000, 3),
            },
        )

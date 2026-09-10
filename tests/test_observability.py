import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.observability import JsonFormatter, MetricsRegistry, ObservabilityMiddleware
from api.services.background_jobs import BackgroundJobCoordinator


def test_request_ids_logs_and_metrics_use_route_templates(caplog):
    metrics = MetricsRegistry()
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware, metrics=metrics)

    @app.get("/items/{item_id}")
    def item(item_id: str):
        return {"id": item_id}

    with (
        caplog.at_level(logging.INFO, logger="dayfinch.request"),
        TestClient(app) as client,
    ):
        response = client.get(
            "/items/private-record-id?secret=never-log-this",
            headers={"X-Request-ID": "upstream-request-123"},
        )

    assert response.headers["X-Request-ID"] == "upstream-request-123"
    rendered = metrics.render()
    assert 'route="/items/{item_id}"' in rendered
    assert "private-record-id" not in rendered
    assert "never-log-this" not in rendered
    record = next(
        record for record in caplog.records if record.name == "dayfinch.request"
    )
    assert record.route == "/items/{item_id}"
    assert record.request_id == "upstream-request-123"


def test_invalid_request_id_is_replaced():
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware, metrics=MetricsRegistry())

    @app.get("/")
    def index():
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/", headers={"X-Request-ID": "bad id\nvalue"})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] != "bad id\nvalue"
    assert len(response.headers["X-Request-ID"]) == 36


def test_metrics_registry_can_replace_an_absolute_gauge():
    metrics = MetricsRegistry()
    metrics.set_gauge("dayfinch_readiness", 1)
    metrics.set_gauge("dayfinch_readiness", 0)
    assert "dayfinch_readiness 0" in metrics.render()


def test_json_formatter_emits_structured_scalar_fields_only():
    record = logging.LogRecord(
        "dayfinch.test", logging.INFO, __file__, 1, "event", (), None
    )
    record.request_id = "request-123"
    record.private_object = {"password": "must-not-render"}
    payload = json.loads(JsonFormatter().format(record))
    assert payload["event"] == "event"
    assert payload["request_id"] == "request-123"
    assert "private_object" not in payload


class JobDatabase:
    def __init__(self, claimed=True):
        self.claimed = claimed

    def claim_background_job(self, *_args):
        return self.claimed

    def release_background_jobs(self, *_args):
        return 0


def test_background_job_metrics_cover_success_skip_and_failure():
    metrics = MetricsRegistry()
    successful = BackgroundJobCoordinator(JobDatabase(), "owner", metrics)
    assert successful.run("reports", 60, lambda: 42) == 42

    skipped = BackgroundJobCoordinator(JobDatabase(False), "owner", metrics)
    assert skipped.run("reports", 60, lambda: 99) is None

    with pytest.raises(RuntimeError):
        successful.run("retention", 60, lambda: (_ for _ in ()).throw(RuntimeError()))

    rendered = metrics.render()
    assert 'job="reports",outcome="succeeded"' in rendered
    assert 'job="reports",outcome="lease_skipped"' in rendered
    assert 'job="retention",outcome="failed"' in rendered

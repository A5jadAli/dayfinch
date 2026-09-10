import json
from pathlib import Path

MONITORING = Path(__file__).parents[1] / "deploy" / "monitoring"


def test_monitoring_rules_cover_required_signals():
    rules = (MONITORING / "dayfinch-alerts.yaml").read_text()
    for alert in (
        "DayfinchTargetDown",
        "DayfinchNotReady",
        "DayfinchHighErrorRate",
        "DayfinchHighMeanLatency",
        "DayfinchBackgroundJobFailures",
        "DayfinchQueueBacklogHigh",
        "DayfinchScreenshotIngestFailures",
    ):
        assert f"alert: {alert}" in rules
    for metric in (
        "dayfinch_readiness",
        "dayfinch_http_requests_total",
        "dayfinch_http_request_duration_seconds_sum",
        "dayfinch_background_jobs_total",
        "dayfinch_queue_backlog",
    ):
        assert metric in rules


def test_grafana_dashboard_is_valid_json_with_unique_panels():
    dashboard = json.loads(
        (MONITORING / "dayfinch-grafana-dashboard.json").read_text()
    )
    assert dashboard["uid"] == "dayfinch-operations"
    panels = dashboard["panels"]
    assert len({panel["id"] for panel in panels}) == len(panels)
    titles = {panel["title"] for panel in panels}
    assert {
        "Readiness",
        "HTTP 5xx ratio",
        "Mean request latency",
        "Background job failures",
        "Durable queue and outbox backlog",
        "Screenshot ingest failures",
    } <= titles
    expressions = "\n".join(
        target["expr"] for panel in panels for target in panel["targets"]
    )
    assert "dayfinch_queue_backlog" in expressions
    assert 'route=\"/api/v1/activity\"' in expressions

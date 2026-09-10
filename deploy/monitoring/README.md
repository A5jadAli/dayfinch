# Dayfinch monitoring starter kit

These files are vendor-neutral inputs for any Prometheus-compatible scraper and
Grafana installation:

- `dayfinch-alerts.yaml` contains starter alerting rules.
- `dayfinch-grafana-dashboard.json` is an importable dashboard with a selectable
  Prometheus data source and instance filter.

Set a unique 32-or-more-character `TRACKER_METRICS_BEARER_TOKEN` on every Dayfinch
replica. Configure Prometheus to scrape `/metrics` with that bearer token, use the
job label `dayfinch`, and probe `/readyz` at least every 30 seconds. Do not put the
token in this directory or the Grafana dashboard.

The dashboard and alerts cover HTTP error ratio, mean latency, worker failures,
durable queue/outbox depth, readiness, in-flight requests, and screenshot-ingest
server errors. Dayfinch currently exposes duration sum and count rather than
histogram buckets, so latency panels and alerts are arithmetic means, not p95/p99.

`dayfinch_queue_backlog` reports aggregate server-side Jira worklog, Asana comment,
and Slack notification queues. A desktop agent's encrypted offline queue remains
on that device and cannot be centrally measured until it reconnects. If backlog
collection cannot query PostgreSQL, `dayfinch_metrics_collection_success` becomes
zero while the request counters remain scrapeable.

The alert thresholds are conservative examples, not production SLOs. Tune them to
measured baseline traffic and route criticality, and route warning/critical labels
through the deployment's notification system. First-response actions are in
`docs/runbook.md` once the operator handoff item is installed.

The rules were checked with `promtool` from the official Prometheus 3.13.3 image:

```bash
docker run --rm --entrypoint promtool \
  -v "$PWD/deploy/monitoring:/work:ro" \
  prom/prometheus:v3.13.3 check rules /work/dayfinch-alerts.yaml
python -m json.tool deploy/monitoring/dayfinch-grafana-dashboard.json >/dev/null
```

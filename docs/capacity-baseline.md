# Local single-host capacity baseline

> **Local single-host baseline, not a production capacity claim.** This run used
> local test-only identities, Mailpit, and MinIO. It does not establish a release
> capacity envelope, production SLO, multi-replica behavior, or cloud S3 latency.

## Environment and workload

Measured on 2026-09-10 on one Linux workstation:

- Intel Core i5-5300U, 2 cores / 4 threads; 7.6 GiB host RAM.
- Docker Engine 29.6.1 and Docker Compose 5.2.0.
- One Dayfinch server container, PostgreSQL 17 Alpine, and local MinIO with bucket
  versioning enabled. Docker reported a 1.826 GiB container memory ceiling.
- Default Dayfinch database pool: minimum 1, maximum 10 connections.
- Ten independent disposable employees and desktop device tokens.
- 600 seconds; heartbeat every 15 seconds; one versioned MinIO screenshot upload
  per device every 15 seconds.
- One authenticated administrator reader cycled `/`, `/timesheets`, and `/reports`
  with a one-second pause after each three-page cycle.
- Background retention, report, timesheet, integration, payroll, and outbox jobs
  remained enabled as they are in the local application.

Command shape (the password was supplied only through the named environment
variable and device tokens through a mode-0600 temporary file):

```bash
export TRACKER_LOAD_TEST_PASSWORD="$TRACKER_ADMIN_PASSWORD"
.venv/bin/python scripts/load_test.py \
  --base-url http://127.0.0.1:8000 \
  --tokens-file /tmp/dayfinch-wi5-tokens \
  --duration 600 --heartbeat-interval 15 --capture-interval 15 \
  --web-email "${TRACKER_ADMIN_EMAIL:-admin@example.local}" \
  --read-interval 1 --timeout 15
```

## Result

The accepted combined run completed in 600.168 seconds:

- 2,436 requests; 4.059 requests/second overall.
- 0 errors and 0.0% request error rate; no 4xx, 5xx, or transport failures.
- 410 successful heartbeat requests, including final stopped heartbeats.
- 400 successful screenshot/activity writes to versioned local MinIO.
- 542 successful reads for each web page (1,626 authenticated reads total).

| Route | Count | p50 | p95 | p99 | Max |
| --- | ---: | ---: | ---: | ---: | ---: |
| Dashboard `/` | 542 | 58.569 ms | 85.634 ms | 247.303 ms | 370.367 ms |
| Timesheets `/timesheets` | 542 | 21.695 ms | 34.846 ms | 91.855 ms | 206.058 ms |
| Reports `/reports` | 542 | 17.251 ms | 30.007 ms | 47.440 ms | 134.916 ms |
| Heartbeat `/api/v1/heartbeat` | 410 | 137.267 ms | 308.740 ms | 446.205 ms | 465.124 ms |
| Screenshot `/api/v1/activity` | 400 | 148.201 ms | 346.473 ms | 490.381 ms | 501.285 ms |

PostgreSQL was sampled every two seconds for 500 seconds (245 successful samples).
The maximum observed server-side Dayfinch connection count was 10 and the maximum
observed `active` count was 2. The application does not currently export pool wait
duration, so saturation/wait was not directly observable. Reaching 10 connected
sessions shows the configured pool expanded to its maximum; it does not prove all
connections were busy.

A point-in-time resource sample during the combined run reported:

| Container | CPU at sample | Memory at sample |
| --- | ---: | ---: |
| Dayfinch server | 6.03% | 142.3 MiB |
| PostgreSQL | 6.70% | 348 MiB |
| MinIO | 0.49% | 91.53 MiB |

These are individual observations, not maxima or sizing recommendations.

## Findings and bottlenecks

- Screenshot and heartbeat writes were the slowest paths. Their p95 values were
  346.473 ms and 308.740 ms, respectively, during synchronized ten-device bursts.
  Screenshot latency includes request parsing, PostgreSQL work, and a versioned
  MinIO object write.
- Dashboard median latency was higher than the two list pages, and its p99 rose to
  247.303 ms during mixed write traffic. The small local dataset and zero-error run
  did not expose a clear N+1 defect worth changing in this scoped pass.
- MinIO was not CPU-bound in the sampled instant. PostgreSQL and the application
  did more work, but pool-wait telemetry and continuous CPU maxima would be needed
  to attribute the tail precisely.
- No request throttles fired. The web cadence stayed well under the conservative
  signed-in limit, and each device stayed below its replay allowance.
- The harness itself had one clear defect: its prior five-second heartbeat payload
  violated the API's 15-second minimum and would produce 422 responses. The default
  and emitted value are now aligned at a minimum of 15 seconds, with regression
  coverage.

Before a production capacity claim, repeat in a production-like environment with
agreed concurrency and SLOs, representative data volume, continuous CPU/memory and
pool-wait telemetry, cloud object storage, peak and 2×-peak stages, retention load,
and replica/failure scenarios.

# Local failure drills

> **Local-only resilience evidence, not a production failover claim.** These drills
> use the opt-in Mailpit and MinIO stand-ins, one local server, one local PostgreSQL
> container, a disposable employee, and a synthetic JPEG. The drill captures no
> screen or user activity and cannot target a non-loopback Dayfinch origin.

## Automated procedure

Start the local stand-in stack described in [LOCAL_TESTING.md](LOCAL_TESTING.md),
then run from the repository root:

```bash
set -a
source .env
set +a
.venv/bin/python scripts/local_failure_drills.py \
  --admin-email "${TRACKER_ADMIN_EMAIL:-admin@example.local}" \
  --acknowledge-local-disruption
```

The acknowledgement is mandatory because the script stops and starts Compose
services. It restores each dependency before moving to the next scenario, never
prints the admin password or generated device token, and stores the temporary
agent queue under the operating system's temporary directory. It creates a
disposable invitation, employee, project, device, work session, and two activity
records in the local database so that the recovered data can be inspected later.

The script performs this sequence:

1. Stop Mailpit, create an invitation through the signed-in web UI, verify that the
   UI reports failure and still displays a valid one-time link, then start Mailpit.
2. Accept the retained invitation, create/assign a project, enroll a desktop
   device through the UI, and send an active heartbeat.
3. Put a synthetic screenshot in the real encrypted desktop queue, stop MinIO,
   attempt upload, verify the queue still contains one item, start MinIO, retry,
   and verify the queue becomes empty.
4. While the work session is active, stop PostgreSQL, send another journaled
   heartbeat, verify it remains queued and `/readyz` returns `503`, start
   PostgreSQL, replay the heartbeat, and verify the journal becomes empty.
5. Queue another synthetic screenshot, stop the Dayfinch server, attempt upload,
   verify the queue still contains one item, start the server, retry, and verify
   the queue becomes empty.
6. Send the final stopped heartbeat and sign back in as the administrator. Both
   recovered screenshot IDs must be present on the activity page.

## Accepted run: 2026-09-10

The local stack used PostgreSQL 17 Alpine, Mailpit 1.31.1, the pinned local MinIO
release in `compose.local.yaml`, and the locally built Dayfinch image. The complete
run exited zero.

| Scenario | Expected | Actual | Elapsed |
| --- | --- | --- | ---: |
| SMTP unavailable during invitation | Invitation remains usable and the UI reports delivery failure. | The UI displayed `Email delivery failed`; the displayed one-time URL returned HTTP 200 before acceptance. Mailpit was restored. | 0.240 s |
| MinIO unavailable during upload | The encrypted agent queue retains the capture and retries without loss. | Upload raised an HTTP error; queue count stayed at 1. After MinIO recovery, retry returned 201, queue count became 0, and the record appeared in admin activity. | 25.354 s |
| PostgreSQL restart during active tracking | The heartbeat remains journaled, readiness fails, and replay succeeds. | The active heartbeat received an HTTP error and stayed queued; `/readyz` returned HTTP 503 with `{"status":"unavailable"}`. After restart, replay succeeded, queue count became 0, and the work session accepted a final stopped event. | 6.449 s |
| Server restart during upload | The encrypted agent queue retains the capture and retries without loss. | Upload raised a connection error; queue count stayed at 1. After server recovery and a ready response, retry returned 201, queue count became 0, and the record appeared in admin activity. | 9.882 s |

`/readyz` deliberately checks PostgreSQL, the dependency required for application
consistency. It remained ready while only MinIO was unavailable; the screenshot
endpoint surfaced that storage failure and the agent queue provided retry safety.
The server is unreachable, rather than able to emit readiness, while its container
is stopped.

## Findings

- No Dayfinch product defect remained after the accepted run. Existing encrypted
  queue semantics preserved both screenshots and the heartbeat until acknowledgement.
- The first draft of the drill queued a screenshot before starting a work session.
  The API correctly returned `409 No tracked work session`; the drill was corrected
  to establish active tracking before the accepted run.
- This exercise validates process/dependency restart recovery on one workstation.
  PostgreSQL failover, multi-replica behavior, cloud object storage, and production
  SMTP recovery still require operator-owned infrastructure and are not claimed.

Unit coverage for the loopback restriction, explicit disruption acknowledgement,
and exact local Compose file selection is in `tests/test_local_failure_drills.py`.
The existing offline-resilience and invitation-delivery tests cover queue replay
ordering and explicit SMTP exception reporting without disrupting services.

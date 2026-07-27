# Dayfinch architecture

## Server boundaries

- `main.py` is the composition root. It creates infrastructure, middleware,
  background services, and route modules; it contains no product workflows.
- `routers/` owns HTTP validation and response rendering by domain.
- `repositories/` owns SQL by domain. `Database` remains a small compatibility
  facade and connection/transaction owner.
- `services/` coordinates workflows that cross persistence and external systems.
- `migrations.py` contains ordered, idempotent schema changes recorded in
  `schema_migrations`.
- `web.py` centralizes session authentication, CSRF checks, and resource policies.
- `storage.py` abstracts private local and S3 screenshot storage.

## Agent boundaries

- `activity.py` counts aggregate interaction and foreground-focus duration.
- `capture.py` captures and encodes screenshots.
- `queue.py` provides durable, bounded, migration-safe offline persistence.
- `client.py` owns the server protocol.
- `main.py` currently coordinates scheduling and visible tray state. Scheduling,
  session state, and tray presentation should be separated before adding dynamic
  task selection or automatic updates.

## Tracking invariants

1. A device belongs to one project at capture time; a user may belong to many.
2. At most one active or paused work session exists per device.
3. Paused time is excluded using explicit work-session segments.
4. New captures store immutable user, project, task, and session attribution.
5. Focus duration and physical interaction duration are separate signals.
6. Screenshot storage is deleted before its database record, so storage failure
   never leaves metadata claiming a successful deletion.
7. Device and invitation secrets are stored only as cryptographic hashes.

## Next architecture steps

- Add a `TimesheetService` for submission, corrections, locking, and approval.
- Split agent scheduling/session coordination from tray presentation.
- Replace SQLite with PostgreSQL repositories for production deployments.
- Stream large objects and move exports/retention to a dedicated worker process.
- Add structured telemetry, dependency locking, CI quality gates, and load tests.

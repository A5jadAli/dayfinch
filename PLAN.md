# Dayfinch: Product and Engineering Plan

## 1. Product boundary

Build a transparent, consent-based tracker for company-managed Windows, macOS,
and Linux computers. The desktop agent is always visible, can be paused by the
employee, and sends one screenshot plus activity totals every 10 minutes.

The MVP records:

- screenshot captured at a configurable interval (default: 600 seconds);
- keyboard event count, never key values or typed text;
- mouse click count and approximate movement distance;
- the foreground application name (window titles are disabled by default);
- device heartbeat, agent version, operating system, and pause state.

The MVP deliberately does not include hidden operation, keylogging, clipboard
collection, webcam/microphone access, browser history, remote control, or file
inspection.

## 2. Roles and core workflows

### Administrator

1. Sign in to the private dashboard.
2. Add an email and privately share its one-time invitation link.
3. View all devices, last-seen state, screenshot timeline, and activity totals.
4. Disable a device token or delete old evidence according to policy.

### Employee/device owner

1. Accept the admin invitation, set a password, and enroll a device.
2. Install the visible desktop agent and explicitly enable tracking.
3. View only their devices/captures and delete a complete 10-minute interval.
4. See whether tracking is active, paused, offline, or uploading.
5. Pause/resume tracking from the tray menu and quit it normally.

## 3. Architecture

```text
Windows / macOS / Linux agent
  - visible tray status
  - screenshot + aggregate activity counters
  - encrypted HTTPS upload
  - bounded local offline queue
                |
                v
FastAPI service
  - administrator and invited-member session authentication
  - hashed device bearer tokens
  - upload validation and rate limits
  - SQLite + local files for development; private S3-compatible storage supported
                |
                v
Server-rendered administrator dashboard
  - admin email invitations and account list
  - owner-scoped devices and screenshot/activity timeline
  - enrollment, revocation, and full-interval deletion
```

Production replaces SQLite/local files with PostgreSQL and private S3-compatible
object storage. TLS terminates at a reverse proxy or managed ingress. Screenshot
objects are never public.

## 4. Delivery milestones

### Milestone A: local MVP

- Backend database schema, authentication, device enrollment, ingest API.
- Admin device list and screenshot timeline.
- Cross-platform agent with consent, pause/resume, capture, counters, app name,
  retries, and bounded offline queue.
- Unit tests, Docker development deployment, and packaging instructions.

### Milestone B: pilot hardening

- PostgreSQL and private S3 storage.
- Per-organization encryption keys, audit log, CSRF protection, request limits,
  retention worker, and backup/restore drills.
- Signed Windows/macOS/Linux installers and auto-update channel.
- Pilot with a small, informed group; validate CPU, memory, bandwidth, permissions,
  screenshot quality, and false offline alerts.

### Milestone C: production

- Multi-tenant organizations and role-based access.
- SSO/MFA, immutable admin audit trail, legal-policy acknowledgements.
- Screenshot redaction/exclusion rules and regional retention controls.
- Monitoring, alerting, capacity planning, penetration test, and incident runbook.

## 5. Security and privacy requirements

- Obtain documented, informed consent and comply with employment/privacy law in
  every jurisdiction where the agent runs.
- Use HTTPS outside localhost. Never send a device token over plaintext networks.
- Store only SHA-256 token hashes server-side; show enrollment tokens once.
- Keep screenshots in private storage and authorize every download.
- Use least privilege. The agent must not require administrator/root permissions.
- Default retention target: 30 days; make it configurable and automatically purge.
- Record admin viewing, export, token creation, revocation, and deletion in an audit
  log before production rollout.
- Provide employee access/correction/deletion processes where law requires them.

## 6. Acceptance criteria for the MVP

- Agent runs on all three OS families and remains visibly controllable.
- No key value is retained in memory beyond the input callback argument.
- A network outage does not lose recent records and cannot grow disk use without
  bound.
- Duplicate upload retries are idempotent.
- An invalid/revoked token cannot upload or retrieve data.
- Screenshot files cannot be fetched without an authenticated admin session.
- Admin can enroll and revoke a device and filter its timeline.
- Automated tests cover token verification, ingest validation, queue behavior, and
  pause-state behavior.

## 7. Decisions needed before a real rollout

- Countries/states involved and approved employee notice/consent language.
- Screenshot retention period and who may view/export screenshots.
- Whether foreground app names are necessary; full window titles should remain off
  unless legal and security review specifically approves them.
- Hosting region, expected device count, SSO provider, and data-loss policy.
- Allowed work schedule and employee-controlled privacy/pause periods.

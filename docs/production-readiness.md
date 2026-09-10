# Dayfinch production-readiness register

This is a release gate, not a marketing checklist. A capability is complete only
when its user workflow, authorization boundary, failure behavior, observability,
and automated/manual verification are all evidenced.

## Current release decision

**Not yet approved for production.** The local deployment is suitable for product
testing. Desktop onboarding, canonical public URLs, SMTP invitations, distributed
login throttling, readiness probes, hardened containers, and browser security
headers are implemented. The blockers below remain authoritative.

## Blockers

| Priority | Requirement | Evidence required to close |
| --- | --- | --- |
| Closed | Complete scoped authorization | Every browser mutation route is guarded by session/pre-auth plus CSRF (with explicit bearer/HMAC exceptions). Cross-scope tests cover organization/project roles, Manage-IT, team leads, payroll, tracking, schedules, expenses, activity deletion, and lifecycle downgrades. Repository enforcement stops active work when a member/project loses tracking eligibility. |
| P0 | Production identity lifecycle | OIDC login, existing-account linking, signed token validation, PKCE/state/nonce, domain enforcement, local logout, fixed-origin RP-initiated OIDC logout, SCIM user lifecycle, and SCIM Group/team synchronization with manual-mutation protection are implemented. SAML adds signed SP-initiated requests, locally pinned and bounded metadata, current RSA certificate/key-usage enforcement, hardened XML-signature verification with allow-listed SAML structures, signed response plus assertion validation, fixed audience/destination, request/RelayState correlation, two-hour maximum assertions, PostgreSQL replay IDs, existing-account-only linking, and signed metadata. Disposable OIDC/SAML/SCIM provider acceptance and signing-key/certificate rotation drills remain. |
| P0 | Desktop distribution | Deterministic portable filenames, SHA-256 checksums, build provenance, stable tag publishing, Ed25519-signed manifests, pinned public keys, platform/version selection, verified staging, unchanged-target checks, private rollback copies, atomic replacement, post-install diagnostics, durable recovery states, automatic rollback, private first-run enrollment import, native Linux `.deb`, Windows Inno Setup, and macOS component-package builders are implemented. Tagged releases fail closed without Authenticode and Developer ID credentials; the workflow signs and verifies both Windows artifacts, enables the macOS hardened runtime, notarizes/staples/validates the `.pkg`, and preserves bundle signing by requiring package-level macOS upgrades. Close this gate only after a certificate-backed tagged run, a signed APT repository, and real-device install/update/rollback acceptance. |
| Closed | Worker scalability | PostgreSQL leases make retention, scheduled-report delivery, and timesheet generation singleton across API replicas, with graceful release and expiry-based crash failover. |
| P0 | Backup and restore | Encrypted PostgreSQL plus exact-version object backup/restore, payload hashes, safe extraction, overwrite refusal, database-name confirmation, and key escrow instructions are implemented. Schedule it in the target platform, retain an off-site copy, and record a representative restore drill proving the chosen RPO/RTO before release. |
| P0 | Platform acceptance | Pass the real Windows/macOS/X11/Wayland matrix in `testing-playbook.md`; CI mocks alone are insufficient for capture permissions and sleep/wake behavior. |
| P1 | Provider integrations | The GitHub App issue connector implements OAuth/PKCE installation ownership verification, least scopes, ephemeral user-token revocation, short-lived in-memory installation tokens, pagination, bounded retries/rate limits, signed idempotent webhooks, worker-safe claims, incremental synchronization, daily reconciliation, and contract tests. Jira Cloud implements resource-restricted OAuth 3LO, encrypted rotating site/member credentials, bounded issue synchronization, two-pass reconciliation, and durable per-member worklog delivery with authorization windows and lost-response recovery. Asana implements least-scope OAuth/S256 PKCE, multi-workspace selection, encrypted rotating site/member credentials, bounded leased project/task/subtask synchronization, assignee-scoped visibility, two-pass removal, and durable per-member UTC-day comments with authorization windows, update/removal, duplicate detection, and lost-response recovery. Slack implements OAuth v2, encrypted rotating bot credentials, verified channel/person selection, organization/per-member rules, transactional timer/to-do outbox events, ordered retry-safe posting, stable client message IDs, replica leases, retention, and revocation. Disposable real-provider acceptance, provider permission/revocation cases, and secret/key-rotation drills remain for all four. PayPal and Wise require disposable sandbox/live acceptance covering SCA or approval, insufficient balance, lost responses, duplicate requests, returns, refunds, chargebacks, and credential rotation. Direct bank, accounting, and payroll-vendor connectors remain. |
| P1 | QuickBooks acceptance | The Intuit Timer Activity IIF mapping/export workflow is implemented against the official import-kit layout. Import a fixture and a representative approved pay period into a disposable copy of the exact supported QuickBooks Desktop edition being deployed; reconcile employee/day/customer/service/class/duration/billable totals and record the result before release. |
| P1 | Observability | Privacy-safe JSON request/job logs, validated/returned correlation IDs, protected Prometheus request latency/count/in-flight, readiness, durable queue depth, and job outcome/latency metrics are implemented. Vendor-neutral starter alerts and a Grafana dashboard cover the required signals, and the owner/manager audit-log report has indexed filters, bounded pagination, CSV/PDF export, and configurable retention. Deployment-specific aggregation/routing, exception sink, tuned thresholds, and production retention/export evidence remain. |
| P1 | Capacity evidence | A guarded multi-device harness now measures real heartbeat/session and multipart screenshot ingestion with status/error rates and p50/p95/p99/max latency. Representative environment results, dashboard/report workloads, PostgreSQL pool sizing, S3 latency, retention batches, and an agreed capacity envelope remain. |
| P1 | Disaster/failure exercises | PostgreSQL failover, S3/KMS denial, disk full, SMTP outage, queue saturation, clock skew, and network partition drills. |
| P0 | Native mobile distribution | The Expo/React Native iOS/Android tracker implements one-time HTTPS enrollment, Keychain/Keystore credential storage, a bounded SQLCipher offline queue, ordered idempotent timer replay, project/task selection, reminders, explicit location consent, foreground fallback, and background GPS restricted to active timers. Validation covers TypeScript, pure contract tests, dependency audit, Expo Doctor, both platform bundles, native generation, and Android compilation in CI. Close this gate with operator-owned Apple/Google/EAS projects, privacy disclosures, signed store builds, and the real-device/OS acceptance matrix below. |

### Backup/restore evidence

The 2026-09-10 local drill produced a 2,498,597-byte encrypted archive and restored
it after deleting/recreating an isolated PostgreSQL database and versioned MinIO
bucket. Exact inventories matched across 69 tables, 1,860 rows, and 802 referenced
screenshot objects; backup took 10.331 seconds and restore took 19.675 seconds.
Authentication, hash, path-safety, exact S3-version, and target-confirmation cases
are automated. An opt-in destination override and systemd schedule example now
exist. This closes local mechanics, but not the release blocker: production still
needs an escrowed key, scheduled off-site destination/retention/alerts, and a
representative environment drill proving the chosen RPO/RTO.

## Deployment invariants already enforced

- `TRACKER_ENVIRONMENT=production` rejects placeholder admin/session values,
  non-HTTPS public URLs, insecure cookies, wildcard hosts, and a public hostname
  missing from `TRACKER_ALLOWED_HOSTS`.
- Invitation and enrollment links/configuration use `TRACKER_PUBLIC_URL`, not an
  attacker-controlled Host header.
- Login throttling is stored in PostgreSQL and shared by API replicas.
- OIDC accepts only provider-verified email claims from the configured issuer and
  workspace domain, and never auto-creates an account from an IdP response.
- Password and two-factor failures use shared PostgreSQL throttling across replicas.
- SCIM requires a deployment-managed bearer token, enforces the workspace domain,
  revokes devices on deactivation, and cannot modify owner/manager accounts.
- Screenshots and app/domain samples are accepted only when their timestamp falls
  inside a tracked work segment; role downgrade, membership removal, project archive,
  and account disablement synchronously close open segments and breaks.
- Per-member screenshot, blur, app/domain, idle-timeout, and deletion overrides
  inherit organization defaults, are served to enrolled agents, and are enforced
  again during server-side ingestion and deletion authorization.
- Allowed-app policies can require desktop tracking globally or per member. The
  web/field API, geofence auto-start, and repository transaction all enforce the
  effective policy; policy mutation and virtual-device start use advisory locks,
  and transitions close only sessions that are disallowed after inheritance.
- Standard desktop tracking is stopped by default. Automatic fixed-schedule and
  published-shift policies require a member response, are evaluated in the member's
  local timezone, and cannot create concurrent live timers on multiple devices.
- Wayland screenshot capture requests persistent XDG ScreenCast permission, reads
  only the portal-restricted PipeWire stream, and stores each newly rotated
  single-use restore token encrypted in the device queue before frame conversion.
  Permission revocation and monitor loss return control to the portal consent UI.
- Periodic jobs use PostgreSQL leases, so API replicas cannot perform the same
  scheduled work concurrently.
- Direct PayPal and Wise payroll atomically claim each run, snapshot an explicitly
  owner-confirmed recipient, reuse stable provider idempotency keys, bound and
  validate provider responses, and reconcile pending payouts under a singleton
  database lease. Paid direct-provider records receive indexed daily follow-up for
  90 days; returns, refunds, and chargebacks become terminal reversed records and
  are excluded from paid totals. Provider references and reversed records are
  immutable, and paid provider-managed records cannot be manually downgraded.
  Wise additionally authenticates `transfers#state-change`, payout-failure, and
  refund webhooks with the environment-specific RSA key, bounds raw requests,
  deduplicates delivery UUIDs, rejects cross-profile events, ignores stale
  `occurred_at` timestamps, and durably revives canonical reconciliation even
  after the polling window. Production rollout must create all three schema
  `4.0.0` subscriptions before sending the first transfer.
- GitHub integration workers atomically claim due installations. Routine runs use
  overlapping incremental cursors; daily full reconciliation repairs missed events
  without repeatedly scanning complete issue history every five minutes.
- Asana integration workers process at most ten project mappings per leased cycle,
  resume remaining mappings immediately without starving other integrations, and
  advance the five-minute schedule only after a complete pass. Comment delivery
  uses a separate bounded, member-scoped durable outbox.
- Persistent OAuth provider credentials use an integration-specific AES-256-GCM
  keyring, provider/account-bound authenticated data, bounded payloads, online
  primary-key rewrapping, revision checks, and expiring database refresh leases.
  Rewrap work is singleton across replicas and one corrupt row cannot block others.
- `/livez` reports process liveness; `/readyz` verifies PostgreSQL connectivity.
- Dashboard HTML is non-cacheable and receives CSP, frame, MIME, referrer,
  permissions, and production HSTS headers.
- The Compose API container is read-only apart from `/data` and a constrained
  temporary filesystem, drops Linux capabilities, and uses `no-new-privileges`.

## Required production environment

At minimum set:

```dotenv
TRACKER_ENVIRONMENT=production
TRACKER_PUBLIC_URL=https://tracker.example.com
TRACKER_ALLOWED_HOSTS=tracker.example.com
TRACKER_COOKIE_SECURE=true
TRACKER_AUDIT_RETENTION_DAYS=2555
TRACKER_ADMIN_EMAIL=owner@example.com
TRACKER_ADMIN_PASSWORD=<long random secret>
TRACKER_SESSION_SECRET=<at least 32 random characters>
TRACKER_DOCUMENT_ENCRYPTION_KEY=<stable 32-byte base64url key>
TRACKER_INTEGRATION_ENCRYPTION_KEYS=<primary-id:32-byte-base64url-key,older-id:old-key>
TRACKER_BACKUP_ENCRYPTION_KEY=<different escrowed 32-byte base64url key>
TRACKER_METRICS_BEARER_TOKEN=<at least 32 random characters>
TRACKER_STORAGE_BACKEND=s3
TRACKER_S3_BUCKET=<private versioned bucket>
TRACKER_SCIM_BEARER_TOKEN=<at least 32 random characters, if SCIM is enabled>
TRACKER_AGENT_UPDATE_MANIFEST_URL=https://downloads.example.com/dayfinch-update.json
TRACKER_AGENT_UPDATE_PUBLIC_KEY=<base64url Ed25519 public key from the release job>
```

Deploy behind a TLS reverse proxy/load balancer, keep PostgreSQL and object storage
private and use a secrets manager. Before scaling replicas, tune the database pool
budget and test lease failover under the intended deployment topology.

# Dayfinch acceptance and resilience testing

This playbook defines the release gate for the server, desktop timer, field PWA,
encrypted documents, and private storage. A green unit suite is necessary but is
not the only release criterion: the manual platform checks below must also pass on
the operating systems being shipped.

## Automated release gate

Run against a disposable PostgreSQL database whose name ends in `_test`:

```bash
export TRACKER_TEST_DATABASE_URL=postgresql://dayfinch:password@127.0.0.1:5433/dayfinch_test
python -m ruff check api agent tests
python -m compileall -q api agent tests
python -m pytest
npm run build:css
python -m pip wheel . --no-deps --no-build-isolation
```

With metrics enabled, scrape every replica and exercise a 2xx, 4xx, 5xx, successful
job, failed job, and skipped lease. Confirm correlation IDs join proxy and Dayfinch
logs, route labels contain templates rather than record IDs, request/query content is
absent, and alerts fire for readiness failure, sustained 5xx rate, and job failures.

With a local server running, exercise every authenticated page in real Chrome at
desktop and 390px widths, in both themes. The audit fails on body overflow,
missing SVG symbols, unlabeled interactive controls, redirects, or theme errors,
and writes screenshots plus a JSON report to the ignored `.visual-audit/` folder:

```bash
DAYFINCH_AUDIT_EMAIL=admin@example.local \
DAYFINCH_AUDIT_PASSWORD='your local admin password' \
npm run audit:ui
```

The suite covers a route-wide browser authentication/CSRF inventory, organization,
project, team-lead and Manage-IT isolation, two-factor throttling, access-loss session
closure, time-state transitions, offline replay idempotency, restart gaps, idle deduction, queue bounds
and crash cleanup, encrypted queue metadata/screenshots, domain reduction, capture
permissions, synthetic-input flags, S3 versioning/encryption parameters, retention,
tracked-segment-only screenshot/app/domain ingestion, timesheet locking, approval
workflows, encrypted invoice integrity, payroll webhook/PayPal/Wise delivery and
reversal (including Wise RSA signature, replay, profile, event-order, late-refund,
and canonical-reconciliation cases), signatures, and clean migrations.
`tests/test_field_browser.py` additionally drives the production field script in
headless Chrome: variant A has no network and inspects encrypted IndexedDB bytes;
variant B reloads with connectivity, decrypts/uploads the event, and proves the
queue is empty without losing project/task attribution.

## Capacity run

Create a disposable workspace and one enrolled device token per intended concurrent
worker. Run `dayfinch-load-test` for at least 30 minutes at expected peak, then at
2× peak. It creates real sessions and screenshot objects; never point it at employee
production data. Capture its JSON report alongside per-replica Dayfinch metrics,
CPU/RSS, PostgreSQL connections/locks/slow queries, database IOPS/storage, S3
latency/errors, and reverse-proxy saturation.

Set release thresholds before running (request error rate, p95/p99 latency, queue or
pool wait, CPU/memory headroom) and record the largest passing device count. Repeat
with dashboard/report reads, retention and scheduled reports active, then repeat an
S3 latency/503 and PostgreSQL failover scenario. Verify every harness worker sends a
final stopped heartbeat and that retention removes its synthetic captures.

## A/B resilience comparisons

These are controlled behavior comparisons, not marketing experiments. Each pair
must preserve the same time/activity facts after synchronization.

| Pair | Variant A | Variant B | Required invariant |
| --- | --- | --- | --- |
| Connectivity | Online for whole session | Wi-Fi/DNS unavailable, then restored | Same journalled state and capture metadata; B uploads state before screenshots and never stops its timer |
| Upload acknowledgement | Normal 201 response | Response lost after server commit, causing retry | One activity UUID and one stored database record |
| Restart | Graceful quit | Power loss/process kill | Last durable heartbeat caps time; no invented gap and no orphan plaintext screenshot |
| Screenshot policy | 0 screenshots | 3 randomized/10 minutes with blur | Time continues in both; B is blurred before queue/upload |
| Context policy | Apps/domains sampled every ~10 seconds | Apps/domains disabled | Enabled samples replay independently of screenshots; disabled values never reach storage; URL paths, searches and fragments never reach either variant |
| Storage | Local private files | S3 with AES256 or KMS | Authorized reads match; private/no-store metadata and exact-version deletion hold |
| Invoice key | Correct AES key | Wrong key or modified ciphertext | A decrypts; B fails closed without returning partial content |

The online/offline pair is automated in `tests/test_offline_resilience.py`; policy
pairs are in `tests/test_policy_variants.py`; replay/duplicate cases are in
`tests/test_work_sessions.py` and `tests/test_api.py`.

## Manual outage scenario

1. Confirm opening the standard desktop app leaves it in **Not tracking**, then
   choose a project, press Start, and record its session/project/task, visible elapsed time, queue
   count, application, and browser domain.
2. Disable Wi-Fi and DNS for at least 15 minutes. Switch task once, add a work note,
   pause/resume once, and allow at least two screenshot intervals.
3. Confirm the timer remains active, status says `Offline`, elapsed time advances
   only while active, and encrypted `.dfq` files plus state rows increase locally.
   Searching the queue directory must not reveal the work note, app, domain, or JPEG
   signature.
4. Quit and reopen while still offline. Confirm the queue counts survive and the
   timer can continue without a server response.

### Automatic tracking policy matrix

Test both fixed member-local schedules and published shifts. Cover normal and
overnight windows, daylight-saving transitions, app launch before and during the
window, pending/accepted/declined consent, first-activity gating with and without an
OS idle signal, manual Stop suppression for the current window, project removal,
policy deletion, app restart after a manual Stop, sleep across the stop boundary,
and two enrolled devices. An
explicit Start on device B must stop device A; a later routine heartbeat from A must
be rejected and stop A locally instead of alternating ownership or double-counting.

### Member tracking policy matrix

For allowed timer apps, screenshot frequency, irreversible blur, app names, website
domains, idle timeout, and member screenshot deletion, test organization defaults
plus every individual inherit/on/off or custom state. Change a default after saving
an override and confirm inherited fields change while explicit fields do not. For
desktop-only mode, verify web/field start and resume denial, geofence start
suppression, immediate closure of disallowed sessions/segments/breaks, explicit
`all` exemptions, concurrent policy/start serialization, and continued desktop
heartbeat operation. Also verify the desktop configuration response, usage and
screenshot ingestion, deletion authorization, audit event, search/pagination,
disabled users, invalid values, and owner/manager/member access boundaries.
5. Restore connectivity. Confirm state rows drain first, then captures; counts reach
   zero without manual action. In the dashboard verify the task switch, note,
   activity totals, host-only domain, screenshot time, and project attribution.
6. Repeat with the network dropping immediately after an upload. Verify the retry is
   reported as a duplicate and does not create or delete the accepted screenshot.

## Manual platform and edge matrix

- Windows 11, current macOS, X11 Linux and Wayland Linux: start/pause/resume/stop,
  logout/shutdown, sleep/wake, multi-monitor capture, DPI changes, screen lock and
  permission denial/regrant.
- On both current GNOME and KDE Wayland, select a monitor on the first capture and
  verify at least ten later scheduled/manual captures do not re-prompt. Restart the
  agent and repeat using the encrypted restore token. Then revoke portal permission,
  remove/reconnect the selected monitor, and verify a new prompt occurs without
  capture or token reuse before approval.
- Browser bridge: Chrome/Edge/Firefox focus changes, private window, non-HTTP tabs,
  extension restart, wrong token and port collision. Exercise project/task Start,
  Pause, Resume, and Stop from the popup and verify a non-extension origin cannot
  read or mutate timer state. Only a normalized hostname may appear.
- Tracking reminders: disabled/enabled settings, weekday and overnight windows,
  exact start/end boundaries, minimum five-minute recurrence, notification and
  alert modes, timer start/pause/stop, app restart, clock rollback, and encrypted
  local preference storage. A paused or running timer must never show a reminder.
- Custom reports: every role scope, date/member/project filter, selected columns,
  saved and reopened group settings, expanded order, collapsed totals for each
  grouping, duration-weighted activity, empty task/client values, 10,000-source-row
  rejection, invalid grouping, and spreadsheet-formula neutralization.
- Specialized exports: use the same explicit UTC period for the time, activity,
  attendance, and expense CSV/PDF buttons. Verify inclusive end dates, reversed and
  367-day rejection, 10,000-row CSV and 1,000-row PDF overflow errors, project
  existence checks, admin-only access, no-store/nosniff headers, audit rows, empty
  documents, and PDF-capacity `503` behavior.
- PDF and scheduled delivery: valid/empty/oversized documents, control characters,
  concurrent font registration, saturated render capacity (`503` plus `Retry-After`),
  PDF MIME attachments, SMTP/TLS failure, daily/weekly/monthly next occurrences,
  exact UTC minute, pause/resume, leap day, short month, last day, and year rollover.
  Verify previous-day/week/month and rolling completed-day windows, query/output
  limits, privacy-safe failure codes, atomic claims and lease expiry across workers,
  per-schedule failure isolation, capped exponential retry with stable jitter, and
  failure-count reset after recovery.
- Audit log: owner/manager access and member denial; system and user authors;
  action/object/affected-member/date filters; stable newest-first pagination;
  deleted/disabled actors; formula/control characters; empty and 10,001-row CSV;
  empty and 1,001-row PDF; retention boundaries and batches; export audit events;
  and query plans using the actor/action/object plus occurred-at indexes.
- QuickBooks IIF: compare TIMERHDR/HDR/TIMEACT columns with Intuit's import-kit
  sample; verify exact list mappings, ASCII/control/formula rejection, multiline
  note reduction, approved-only gating, accounting-timezone midnight and DST splits, minute rounding,
  23:59 duration splitting, empty output, 10,000-row limits, role denial, CSRF,
  audit totals, and import into a disposable supported QuickBooks company backup.
- GitHub App: use a disposable organization and private/public repositories. Verify
  install ownership OAuth/PKCE, requested scopes, repository mapping and rename,
  issue open/edit/close/delete, ignored pull requests, pagination, incremental
  cursor overlap, daily full reconciliation, webhook duplicate/out-of-order
  delivery, invalid/oversized signatures, rate-limit reset, transient 5xx retry,
  repository access removal, app suspend/delete, mapping reassignment while a timer
  is active, multi-replica claim exclusion, user-token revocation, installation-token
  renewal, and private-key/client/webhook-secret rotation. Confirm no access token
  or secret appears in PostgreSQL, HTML, logs, metrics, audits, or error responses.
- Jira Cloud: use a resource-restricted disposable OAuth 3LO app and site. Verify
  state expiry/replay, authorization cancellation, exact callback origin, required
  `offline_access read:jira-user read:jira-work write:jira-work` scopes,
  zero/multiple accessible-site rejection, distinct subject-bound employee grants,
  project access removal, enhanced-JQL token pagination, repeated/oversized pages,
  ADF descriptions, done/reopened/deleted issues, cursor overlap, two consecutive
  full-scan misses, reconnect full reconciliation, 401 refresh, rotating-token
  replacement, invalid grants, reuse-window recovery, 429/5xx backoff, long-sync
  lease renewal, multi-replica exclusion, disconnect credential deletion, and
  encryption-key rewrap. For worklogs, verify off/hourly/daily-midnight-UTC/delayed
  cutoffs, UTC midnight splitting, completed-segment and approved-manual aggregation,
  source update/deletion, disconnected-time exclusion, Jira permission loss,
  employee reauthorization, external worklog deletion, multi-replica claims, and a
  lost POST response after provider commit. The retry must recover the stable
  `dayfinch.export_id` property and leave exactly one worklog. Confirm tokens and
  provider error bodies never appear in HTML, PostgreSQL text columns, logs,
  metrics, audits, or exception responses.
- Asana: use a disposable OAuth app, account, and workspace with nested subtasks
  and at least two members. Verify S256 PKCE/state expiry and replay, exact least
  scopes, zero/one/multiple workspace selection, project mapping/reassignment,
  assignee changes, completed/reopened/deleted tasks, deep subtasks, repeated and
  oversized pagination, an early 401 refresh, rotating-token replacement, invalid
  grants, 429 `Retry-After`, transient 5xx, ten-project batching, lease expiry and
  multi-replica exclusion. For time comments, verify off/hourly/daily/delayed/
  completed cutoffs, completed tracked plus approved manual aggregation, UTC day
  boundaries, disconnect/reconnect authorization gaps, identity mismatch, source
  update/removal, provider comment deletion, duplicate markers, permission loss,
  and a lost POST response after provider commit. The retry must find the stable
  `dayfinch-export` marker and leave exactly one comment. Revoke site/member grants,
  rotate credential keys, disconnect/reconnect, and confirm tokens/provider bodies
  never appear in HTML, PostgreSQL text columns, logs, metrics, audits, or errors.
- Slack: use a disposable OAuth v2 app and workspace. Verify state expiry/replay,
  cancellation, exact callback origin and bot scopes; public/private channel and
  person discovery; private-channel membership loss; archived/deleted targets;
  organization defaults and every per-member inherit/on/off combination; timer
  start/stop and to-do completion events; pause exclusion; 429 `Retry-After`;
  transient 5xx; token expiry/rotation; early authentication failure; and app
  removal. Lose the response after `chat.postMessage` commits, then verify retrying
  the stable outbox UUID as `client_msg_id` leaves one message. Race two API
  replicas, disconnect/reconnect, rotate credential keys, and confirm tokens and
  provider bodies never appear in HTML, PostgreSQL text columns, logs, metrics,
  audits, or errors.
- Long outage: fill the screenshot limit and verify oldest encrypted screenshots are
  discarded predictably while the smaller state journal continues preserving time.
- Desktop update: install a valid signed upgrade on every packaged OS/architecture;
  tamper with the staged file, alter the installed target after staging, force the
  new binary's diagnostics to fail, make the install directory read-only, and kill
  the helper during each durable state. Verify that the old binary remains or is
  restored byte-for-byte and inspect the plan for `rolled_back` or
  `recovery_required`; never approve a release with an ambiguous `replacing` plan.
- Desktop first run: launch each packaged tracker with no configuration, import a
  valid downloaded `agent.toml`, and verify the platform-specific per-user copy and
  its parent directory are private. Exercise cancel, malformed/oversized file,
  symlink source/destination, an existing configuration, explicit re-enrollment via
  `--import-config`, read-only destination, and abrupt termination during the atomic
  copy. Diagnostics and the in-memory capture test must remain usable before
  enrollment and must not create a configuration.
- Clock and locale: UTC offsets, DST transition, clock five minutes fast, timestamps
  without a timezone, and replay older than 90 days. Invalid/future data must fail
  without poisoning later queue entries; permanently rejected or corrupt items move
  into the encrypted `quarantine/` directory and surface a `Needs attention` status.
- Identity: expired invite, disabled member, changed role, revoked device, wrong
  password, TOTP replay window and CSRF mismatch. Exercise SCIM User and Group
  create/list/filter/replace/patch/delete, add/remove team members, deprovisioning,
  duplicate external IDs, privileged-member denial, and IdP-versus-admin races.
  With a real IdP, verify local logout, RP-initiated provider logout and fixed return
  URI, provider session removal, expired tokens, signing-key rotation, and discovery
  cache recovery.
- SAML: import the signed Dayfinch SP metadata into a disposable IdP, then verify
  SP-initiated login, the configured email attribute and NameID, required response
  plus assertion signatures, exact `InResponseTo`/destination/audience, opaque
  RelayState, unknown/disabled users, clock skew, two-hour lifetime rejection, and
  PostgreSQL replay denial across two API replicas. Rotate IdP certificates with an
  overlap, remove the old certificate, test expiry, and restart to load pinned
  metadata. Tampered, SHA-1, unsigned, unsolicited, XML-entity, oversized, and
  IdP-initiated responses must all fail without disclosing assertion contents.
- Provider credential rotation: connect at least two disposable OAuth accounts,
  prepend a new integration-encryption key, run the rewrap job on two replicas,
  and verify each ciphertext changes exactly once while the refresh token remains
  usable. Tamper with one row, retire a still-used key, expire and race refresh
  leases, and interrupt a rotating refresh inside Atlassian's documented reuse
  window. Require privacy-safe errors and no secret in logs, metrics, HTML, audits,
  PostgreSQL text columns, or process arguments.
- Approvals/payroll: pending manual time/PTO, open session, rejected and approved
  periods, configurable weekly overtime, zero rates, provider timeout, duplicate callback and invalid
  callback signature.
- Storage/database: unavailable PostgreSQL, S3 403/timeout, KMS denial, object
  version replacement, screenshot/app/domain/GPS retention deletion, disk full and
  read-only queue directory.
- Field PWA: denied/approximate GPS, background throttling, encrypted IndexedDB
  queue, offline start/pause/resume/stop ordering, reconnect replay, geofence
  boundary jitter, duplicate location UUID and enter/exit auto-actions.
- Native mobile: current and previous supported iOS plus Android releases on real
  low/mid/high-tier devices; fresh and revoked enrollment; invalid/oversized JSON;
  Keychain/Keystore persistence across restart and exclusion from backup migration;
  SQLCipher at-rest inspection; 10,000-event pressure without timer-transition
  loss; airplane mode, captive portal, 429/5xx, lost responses, clock changes, and
  90-day rejection quarantine. Verify foreground-only and always/background grants,
  approximate GPS, services disabled, permission downgrade/revocation, Android
  foreground notification, iOS location indicator, screen lock, force-stop,
  reboot, Doze/Low Power Mode, OEM battery restrictions, and resumed sync. Location
  must exist only while the timer is active; pause, stop, consent removal, logout,
  and device revocation must halt collection. Confirm app-store privacy labels and
  background-location declarations match observed behavior. iOS/Android must never
  claim or attempt cross-app screenshot or keyboard/mouse capture.
- Invoice vault: create/open/print, S3 and local backends, ciphertext modification,
  missing object, wrong/rotated key and backup restore. Never rotate
  `TRACKER_DOCUMENT_ENCRYPTION_KEY` without re-encrypting existing documents.

Record OS/browser versions, timestamps, expected/actual results, screenshots of the
visible tracker (not captured employee content), and relevant audit IDs. A release
fails if any collected datum exists without disclosure, any offline fact is silently
lost, or any encrypted artifact can be read with the wrong key.

## Backup restore drill

Run this at least quarterly and after PostgreSQL/storage changes. Record the archive
timestamp, size, PostgreSQL/object counts, duration, operator, and resulting RPO/RTO.

1. Put the source workspace in maintenance, create an archive with `dayfinch-ops
   backup --maintenance-confirmed`, move a copy off-site, and restart the source.
2. Provision a disposable PostgreSQL database and isolated local directory or S3
   prefix/bucket. Never point the drill at production storage.
3. Configure the disposable target, stop all target application replicas, and run
   `dayfinch-ops restore --maintenance-confirmed --confirm-database EXACT_DB`.
4. Start one target replica. Require `/readyz` to pass and verify migration count,
   users/projects, sessions, activity rows, invoice rows, and audit history.
5. Sample at least 25 screenshots and every sealed invoice. Verify each opens through
   authorized routes, unauthorized users still receive 404/403, and stored hashes
   match the encrypted manifest. For S3, verify restored rows contain the new object
   VersionIds and retention deletes precisely those versions.
6. Test failure closed: wrong backup key, one flipped ciphertext byte, a modified
   extracted payload, and a mismatched `--confirm-database` must all abort.
7. Destroy only the disposable drill target after evidence is retained. Escrow the
   backup key separately from archives and database/object credentials; test escrow
   recovery with a second authorized operator.

The default production objective is documented by the operator, not hard-coded in
the app. A sensible starting point is a 24-hour RPO and four-hour RTO, tightened
after measuring the representative dataset and business requirement.

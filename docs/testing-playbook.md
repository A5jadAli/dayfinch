# Dayfinch acceptance and resilience testing

This playbook defines the release gate for the server, desktop timer, field PWA,
encrypted documents, and private storage. A green unit suite is necessary but is
not the only release criterion: the manual platform checks below must also pass on
the operating systems being shipped.

## Automated release gate

Run against a disposable PostgreSQL database whose name ends in `_test`:

```bash
export TRACKER_TEST_DATABASE_URL=postgresql://dayfinch:password@127.0.0.1:5432/dayfinch_test
python -m ruff check api agent tests
python -m compileall -q api agent tests
python -m pytest
npm run build:css
python -m pip wheel . --no-deps --no-build-isolation
```

With a local server running, exercise every authenticated page in real Chrome at
desktop and 390px widths, in both themes. The audit fails on body overflow,
missing SVG symbols, unlabeled interactive controls, redirects, or theme errors,
and writes screenshots plus a JSON report to the ignored `.visual-audit/` folder:

```bash
DAYFINCH_AUDIT_EMAIL=admin@example.local \
DAYFINCH_AUDIT_PASSWORD='your local admin password' \
npm run audit:ui
```

The suite covers authentication and CSRF, role/project isolation, time-state
transitions, offline replay idempotency, restart gaps, idle deduction, queue bounds
and crash cleanup, encrypted queue metadata/screenshots, domain reduction, capture
permissions, synthetic-input flags, S3 versioning/encryption parameters, retention,
timesheet locking, approval workflows, encrypted invoice integrity, payroll webhook
signatures, and clean migrations.
`tests/test_field_browser.py` additionally drives the production field script in
headless Chrome: variant A has no network and inspects encrypted IndexedDB bytes;
variant B reloads with connectivity, decrypts/uploads the event, and proves the
queue is empty without losing project/task attribution.

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

1. Start a timer and record its session/project/task, visible elapsed time, queue
   count, application, and browser domain.
2. Disable Wi-Fi and DNS for at least 15 minutes. Switch task once, add a work note,
   pause/resume once, and allow at least two screenshot intervals.
3. Confirm the timer remains active, status says `Offline`, elapsed time advances
   only while active, and encrypted `.dfq` files plus state rows increase locally.
   Searching the queue directory must not reveal the work note, app, domain, or JPEG
   signature.
4. Quit and reopen while still offline. Confirm the queue counts survive and the
   timer can continue without a server response.
5. Restore connectivity. Confirm state rows drain first, then captures; counts reach
   zero without manual action. In the dashboard verify the task switch, note,
   activity totals, host-only domain, screenshot time, and project attribution.
6. Repeat with the network dropping immediately after an upload. Verify the retry is
   reported as a duplicate and does not create or delete the accepted screenshot.

## Manual platform and edge matrix

- Windows 11, current macOS, X11 Linux and Wayland Linux: start/pause/resume/stop,
  logout/shutdown, sleep/wake, multi-monitor capture, DPI changes, screen lock and
  permission denial/regrant.
- Browser bridge: Chrome/Edge/Firefox focus changes, private window, non-HTTP tabs,
  extension restart, wrong token and port collision. Only a normalized hostname may
  appear.
- Long outage: fill the screenshot limit and verify oldest encrypted screenshots are
  discarded predictably while the smaller state journal continues preserving time.
- Clock and locale: UTC offsets, DST transition, clock five minutes fast, timestamps
  without a timezone, and replay older than 90 days. Invalid/future data must fail
  without poisoning later queue entries; permanently rejected or corrupt items move
  into the encrypted `quarantine/` directory and surface a `Needs attention` status.
- Identity: expired invite, disabled member, changed role, revoked device, wrong
  password, TOTP replay window and CSRF mismatch.
- Approvals/payroll: pending manual time/PTO, open session, rejected and approved
  periods, overtime, zero rates, provider timeout, duplicate callback and invalid
  callback signature.
- Storage/database: unavailable PostgreSQL, S3 403/timeout, KMS denial, object
  version replacement, screenshot/app/domain/GPS retention deletion, disk full and
  read-only queue directory.
- Field PWA: denied/approximate GPS, background throttling, encrypted IndexedDB
  queue, offline start/pause/resume/stop ordering, reconnect replay, geofence
  boundary jitter, duplicate location UUID and enter/exit auto-actions.
- Invoice vault: create/open/print, S3 and local backends, ciphertext modification,
  missing object, wrong/rotated key and backup restore. Never rotate
  `TRACKER_DOCUMENT_ENCRYPTION_KEY` without re-encrypting existing documents.

Record OS/browser versions, timestamps, expected/actual results, screenshots of the
visible tracker (not captured employee content), and relevant audit IDs. A release
fails if any collected datum exists without disclosure, any offline fact is silently
lost, or any encrypted artifact can be read with the wrong key.

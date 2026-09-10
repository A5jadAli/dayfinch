# Dayfinch

Dayfinch is a self-hosted workforce operations platform inspired by the
workflows of modern time trackers. It combines a Windows/macOS/Linux desktop timer,
a web timer, activity context, workforce administration, field operations, and
financial workflows under the original Dayfinch brand.

## Current capabilities

- Owner, organization-manager, member, project-manager/project-viewer, and
  privacy-limited Manage-IT access; invitations, teams, project membership, and
  per-member pay/bill rates and daily/weekly limits. Team leads have scoped
  timesheet, manual-time, time-off, schedule, explicitly assigned project/member,
  and expense permissions.
- Desktop and web timers with project/task switching, work notes, breaks,
  idle-time deduction, durable offline replay, and graceful shutdown recovery.
  Organization and per-member app policies can require the desktop tracker;
  policy changes atomically stop disallowed browser/field timers without blocking
  explicitly exempt members.
- Randomized multi-monitor screenshots (0–3 per ten minutes), irreversible
  on-device blur, keyboard/mouse activity levels, independent encrypted app/domain
  sampling, and unusual input signals. Organization defaults can be overridden per
  member for screenshots, blur, apps, domains, idle timeout, and member deletion;
  the effective policy is enforced during ingestion as well as by the agent.
  Dayfinch never records typed keys.
- Dashboard, screenshot gallery, permission-scoped screenshot ZIP archives,
  app/URL summaries, manual-time approvals, timesheet submission/review/locking,
  notifications, saved custom reports, expanded/collapsed date/member/project/client/task
  grouping, formula-safe CSV, bounded PDF exports, matching one-click CSV/PDF
  downloads for time, activity, attendance, and expense reports, and calendar-correct scheduled
  CSV/PDF email delivery. Scheduled exports use completed UTC date windows, enforce
  output limits, atomically claim bounded batches across replicas, isolate failures,
  and retry with capped exponential backoff.
- Owner/manager audit log with author, time, action, object, affected-member and
  detail filters, bounded pagination, formula-safe CSV/PDF export, and configurable
  indexed retention (seven years by default).
- QuickBooks Desktop Timer Activity IIF export with persistent company, employee,
  customer/job, class, and service-item mappings; approved-timesheet gating;
  DST-safe accounting-timezone aggregation; minute rounding; 23:59 row splitting;
  and audit totals.
- Projects, tasks, global/project to-dos, clients, hour/cost budgets, billable
  rates, schedules, attendance, PTO, holidays schema, and expense approvals.
  A least-privilege GitHub App can map repositories to projects and import issues
  as read-only tasks using signed webhooks, overlapping incremental sync, and a
  daily full reconciliation. Jira Cloud OAuth can map accessible Jira projects
  and import issues through bounded enhanced-JQL pagination, overlapping
  incremental sync, and eventual-consistency-safe daily reconciliation. Each
  employee authorizes their own Jira identity for hourly, daily, delayed, or
  disabled worklog synchronization. Asana OAuth supports workspace selection,
  bounded mapped-project task and nested-subtask import, assignee-scoped member
  visibility, two-pass removal reconciliation, and per-member tracked-time comments.
  Slack OAuth notifies selected channels or people when timers start/stop and
  to-dos are completed, with organization defaults and per-member overrides.
- AES-256-GCM sealed invoice documents with tamper detection, invoice status, pay
  rates, member-to-organization invoices with tracked-time generation and partial
  payment records, overtime-aware payroll runs, HMAC-signed payment-provider callbacks,
  and emailed scheduled reports. Direct accounting-provider connectors remain
  hidden until real adapters pass release contracts.
- Installable field PWA with AES-GCM offline timer/location journaling, mobile GPS
  API, job-site geofences, automatic timer actions, enter/exit events, and
  scheduled-versus-worked attendance reports.
- Native iOS and Android tracker built with Expo/React Native: secure one-time
  enrollment, OS keychain/keystore credentials, a SQLCipher offline queue,
  project/task timers, ordered transition replay, local running-timer reminders,
  and explicitly consented foreground/background GPS restricted to active work.
  Mobile OS sandboxing prevents screenshots or keyboard/mouse activity from other
  apps, so those signals remain in the desktop tracker.
- PostgreSQL migrations, audit events, TOTP two-factor login enforcement, secure
  sessions/CSRF, screenshot/app/domain/GPS retention cleanup, and private local or
  S3-compatible storage.
- Privacy by design: no key values, clipboard content, full window titles, full page
  URLs, browser history, audio, webcam recording, or user-file collection. Only the
  domain of the active browser tab is recorded, and only what the running agent
  discloses on start-up is collected.

The UI and terminology are Dayfinch originals; the product follows familiar
workforce-tracker workflows without copying another product's protected branding.
See [docs/hubstaff-parity.md](docs/hubstaff-parity.md) for the researched feature
map and platform-specific constraints.

## Structure

```text
api/        FastAPI routes, services, repositories, PostgreSQL, and security
ui/         Server-rendered templates and static assets
agent/      Desktop timer UI, capture/activity signals, policy sync, and offline queue
extensions/ Authenticated browser timer and host-only active-domain bridge
mobile/     Native iOS/Android timer, secure offline queue, and consented GPS
tests/      API, database, security, storage, agent, and workflow tests
packaging/  Desktop-agent packaging entry point
```

## Run the native mobile tracker

The server must be reachable from the phone through a public HTTPS URL. Create a
device from an assigned project and copy the **Mobile enrollment JSON** shown once
on the enrollment page. Paste that JSON into Dayfinch Tracker; its bearer token is
stored in the OS keychain/keystore and its offline events in SQLCipher.

For a native development build:

```bash
cd mobile
npm ci
npm run doctor
npx expo prebuild --platform android  # use ios on macOS
npx expo run:android                  # or: npx expo run:ios
```

Expo Go is intentionally insufficient because background tasks and SQLCipher need
a development or production native build. Store builds use `mobile/eas.json` and
require the release operator's Apple/Google accounts, signing credentials, EAS
project initialization, privacy declarations, and real-device acceptance. The
mobile CI gate type-checks, tests, audits, exports both platform bundles, generates
Android native sources, and compiles an APK.

## Run locally with Docker

Requirements: Docker with Compose and host ports `8000` and `5433` available.

```bash
cp .env.example .env
# Replace the password/session/database and document-encryption placeholders.
docker compose up --build -d
docker compose ps
curl http://127.0.0.1:8000/health
```

Open <http://127.0.0.1:8000> and sign in as `admin@example.local` with the
`TRACKER_ADMIN_PASSWORD` from `.env`. Follow logs with:

```bash
docker compose logs -f dayfinch-server
```

Stop the app with `docker compose down`. Add `-v` only when you intentionally want
to delete all local PostgreSQL and screenshot data.

## Slack notifications

Create a Slack OAuth v2 app and register
`https://YOUR-DAYFINCH-HOST/integrations/slack/callback`. Grant the bot scopes
`chat:write`, `chat:write.public`, `channels:read`, `groups:read`, and
`users:read`, then set `TRACKER_SLACK_CLIENT_ID`,
`TRACKER_SLACK_CLIENT_SECRET`, and `TRACKER_INTEGRATION_ENCRYPTION_KEYS` in the
server environment. Owners can connect and configure destinations under Settings.
Invite the bot to any private channel before selecting it.

## Encrypted backup and restore

Dayfinch backs up PostgreSQL and every referenced screenshot/sealed invoice into
one authenticated AES-256-GCM archive. Each payload is SHA-256 verified, and S3
objects are read by the exact VersionId recorded in PostgreSQL. Keep
`TRACKER_BACKUP_ENCRYPTION_KEY` in an independent secrets escrow; it must be a
different, stable 32-byte base64url key.

Stop application writes before backup, then run the one-off operations container:

```bash
mkdir -p runtime/backups
docker compose stop dayfinch-server
docker compose run --rm \
  -v "$PWD/runtime/backups:/backups" \
  dayfinch-ops backup \
  --maintenance-confirmed /backups/dayfinch-$(date -u +%Y%m%dT%H%M%SZ).dfbackup
docker compose start dayfinch-server
```

Restore is destructive. Stop the application, take a safety backup, and type the
exact target database name via `--confirm-database`:

```bash
docker compose stop dayfinch-server
docker compose run --rm \
  -v "$PWD/runtime/backups:/backups:ro" \
  dayfinch-ops restore \
  --maintenance-confirmed --confirm-database dayfinch \
  /backups/ARCHIVE.dfbackup
docker compose start dayfinch-server
```

Never test restore for the first time against production. The release drill in
`docs/testing-playbook.md` uses an isolated database and verifies row/object hashes.

## Logs and metrics

Server and periodic-job logs are one-line JSON with a correlation ID, route
template, status, and duration. Request bodies, query strings, raw record IDs,
emails, and client IPs are intentionally excluded. A valid incoming `X-Request-ID`
is preserved; otherwise Dayfinch creates one and returns it on the response.

Set `TRACKER_METRICS_BEARER_TOKEN` to at least 32 random characters to enable the
Prometheus endpoint. It returns 404 when disabled or unauthorized:

```bash
curl -H "Authorization: Bearer $TRACKER_METRICS_BEARER_TOKEN" \
  http://127.0.0.1:8000/metrics
```

Scrape every API replica because counters are process-local. Alerting and exception
aggregation belong in the deployment platform and remain a production release gate.

## Capacity harness

`dayfinch-load-test` exercises the real authenticated heartbeat, work-session, image
upload, storage, activity-row, dashboard, timesheet, and report-read paths. Use only
a disposable workspace because it creates genuine sessions and screenshots. Put
one enrolled device token per line in an ignored file, set a disposable web-reader
password without putting it on the command line, then run:

```bash
export TRACKER_LOAD_TEST_PASSWORD='disposable-account-password'
dayfinch-load-test --base-url http://127.0.0.1:8000 \
  --tokens-file runtime/load-device-tokens --duration 300 \
  --heartbeat-interval 15 --capture-interval 15 \
  --web-email load-reader@example.test --read-interval 1
```

One token represents one concurrent desktop device. Remote targets require HTTPS
and `--acknowledge-production-impact`. Tokens are never printed; the JSON result
contains request rate, status counts, error percentage, and p50/p95/p99/max latency.
Omit `--web-email` only for an ingestion-only comparison. Pair the JSON with
PostgreSQL, S3, CPU/memory, and `/metrics` monitoring.

## Dashboard styles

The server-rendered templates are styled with Tailwind CSS. The compiled
stylesheet `ui/static/app.css` is committed, so running the server, the tests, and
CI needs no Node toolchain, and the dashboard never loads anything from a
third-party CDN.

Rebuild it after editing any template or `ui/tailwind.css`:

```bash
npm install
npm run build:css     # or: npm run watch:css
```

## Run the checks

With the Compose PostgreSQL service running:

```bash
docker compose exec postgres createdb -U dayfinch dayfinch_test
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
export TRACKER_TEST_DATABASE_URL="postgresql://dayfinch:YOUR_POSTGRES_PASSWORD@127.0.0.1:5433/dayfinch_test"
python -m compileall -q api ui agent tests
python -m pytest
```

If the test database already exists, the first command can be skipped. Use the same
database password you placed in `.env`. Tests truncate only `dayfinch_test` and do
not use SQLite; SQLite is limited to the desktop agent's local retry queue.

## Enterprise SSO and SCIM

Set `TRACKER_OIDC_ISSUER`, `TRACKER_OIDC_CLIENT_ID`, and
`TRACKER_OIDC_CLIENT_SECRET` (plus an explicit discovery URL when your provider
requires one), and register this callback with the provider:

```text
https://your-dayfinch-host/auth/oidc/callback
```

Then enable **OpenID Connect** and enter the verified company domain in organization
settings. SSO links only active, invitation-created Dayfinch accounts with a matching
verified email; it does not silently provision unknown users. The flow uses provider
discovery, signed ID-token validation, state, nonce, and S256 PKCE. Set the
provider's exact `end_session_endpoint` in `TRACKER_OIDC_END_SESSION_URL` to send
SSO users through provider logout before returning to Dayfinch. SCIM lifecycle
provisioning is available at `/scim/v2` when `TRACKER_SCIM_BEARER_TOKEN` is set.
Configure the same company domain before connecting the identity provider. SCIM
creates standard members, deactivation immediately revokes their desktop devices,
and Groups synchronize team membership with paginated/filterable create, replace,
patch, and delete workflows. IdP-managed membership is read-only in the admin UI;
privileged owner/manager accounts are intentionally outside SCIM control.

SAML 2.0 is available when an identity provider cannot use OIDC. Register these
service-provider endpoints:

```text
Entity ID / metadata: https://your-dayfinch-host/auth/saml/metadata
Assertion consumer:   https://your-dayfinch-host/auth/saml/acs
```

Set `TRACKER_SAML_IDP_ENTITY_ID` and base64-encode the trusted IdP metadata XML,
the Dayfinch RSA private key, and its matching certificate into the corresponding
`TRACKER_SAML_*_B64` values shown in `.env.example`. Metadata is parsed only from
that deployment value; Dayfinch never fetches an administrator-entered metadata
URL. Then select **SAML 2.0**, the signed email attribute name, and the verified
company domain in Settings.

SAML login is SP-initiated and signs the authentication request with RSA-SHA256.
Strict processing uses SignXML's hardened XML-signature verifier plus a bounded,
allow-listed SAML structure. It requires signed responses and signed assertions,
an exact destination/audience/request ID, an opaque one-time RelayState, a unique
NameID, one signed email value, and a lifetime no longer than two hours. Response
and assertion IDs are consumed transactionally in PostgreSQL, so replicas reject
replay. Unknown accounts are never provisioned. IdP signing certificates are taken
only from pinned metadata, must be current RSA-2048/SHA-256-or-stronger material,
must declare certificate `digitalSignature` key usage, and stop authentication at
expiry until rotated metadata is loaded.

## GitHub issue synchronization

Create a GitHub App and configure the Setup, OAuth callback, and Webhook URLs shown
in `.env.example`. Grant only **Metadata: read** and **Issues: read**, subscribe to
the **Issues**, **Installation**, and **Installation repositories** events, then set
all five required `TRACKER_GITHUB_*` secrets. The integration remains absent from
the reachable UI unless that configuration is complete.

An owner starts installation from **Settings**, completes GitHub's user OAuth/PKCE
verification, maps an accessible repository to a Dayfinch project, and runs the
first synchronization. The short-lived user token is revoked immediately and is
never stored; ongoing requests use one-hour installation tokens held only in
memory. Imported issues are read-only Dayfinch tasks. Signed, idempotent webhooks
apply changes in near real time, five-minute overlapping incremental passes repair
delivery gaps, and a daily bounded full pass reconciles removed issues. Repository
access loss or mapping removal archives imported tasks and stops timers using them.
Disconnecting the app keeps imported tasks as editable ordinary Dayfinch tasks;
reconnecting the same installation safely reattaches their retained origin metadata.

Before production, exercise the connector against a disposable real GitHub
organization using the provider matrix in `docs/testing-playbook.md`; mocked
contract tests do not prove external app configuration or key rotation.

## Jira Cloud issue synchronization

Create one resource-restricted Atlassian OAuth 2.0 (3LO) app, register
`TRACKER_PUBLIC_URL/integrations/jira/callback`, and grant only
`offline_access read:jira-user read:jira-work write:jira-work`. Configure
`TRACKER_JIRA_CLIENT_ID`,
`TRACKER_JIRA_CLIENT_SECRET`, and `TRACKER_INTEGRATION_ENCRYPTION_KEYS`; Jira stays
absent from the UI unless the complete deployment configuration validates.

An owner connects one Jira Cloud site, maps accessible Jira projects to Dayfinch
projects, and runs the first sync. Access and rotating refresh tokens are encrypted
with provider/account-bound AES-256-GCM authenticated data. Refreshes use expiring
database claims and revision checks across replicas. Issue retrieval uses Jira's
enhanced JQL API with a streamed response cap, bounded token pagination, five-minute
cursor overlap, page-by-page lease renewal, and a daily full pass. A missing issue
must be absent from two full passes before Dayfinch archives it, preventing Jira's
documented recent-update lag from stopping a timer after one inconsistent search.
Imported issues are read-only. Each mapped employee links their own Atlassian
identity and chooses off, hourly, daily-at-midnight-UTC, or one-day-delayed worklog
delivery. Completed tracked segments and approved manual time are aggregated by
UTC issue/day. A database outbox uses replica-safe leases, bounded retries,
authorization windows, and a stable Jira worklog property to recover a provider
commit after a lost response without creating duplicate worklogs; subsequent
Dayfinch changes update or delete the owned Jira worklog.

## Asana task synchronization

Create an Asana OAuth app, register
`TRACKER_PUBLIC_URL/integrations/asana/callback`, and grant only `openid profile
email workspaces:read projects:read tasks:read stories:read stories:write`.
Configure `TRACKER_ASANA_CLIENT_ID`, `TRACKER_ASANA_CLIENT_SECRET`, and
`TRACKER_INTEGRATION_ENCRYPTION_KEYS`; Asana remains absent from the UI until the
complete configuration validates.

An owner authorizes Asana, chooses one accessible workspace, maps Asana projects
to Dayfinch projects, and starts synchronization. Tasks and nested subtasks import
as read-only timer tasks. Missing tasks require two successful observations before
archival. Background work uses ten-project batches, PostgreSQL leases, bounded
pagination and response sizes, rate-limit backoff, rotating encrypted refresh
tokens, and a five-minute repair cycle. Disconnecting retains imported tasks as
editable ordinary Dayfinch tasks.

Each employee links their own identity in that workspace. Only tasks assigned to
that Asana identity appear in their timer. Completed tracked segments and approved
manual time can be delivered as stable UTC-day comments in off, hourly, daily,
one-day-delayed, or task-completed mode. Durable outbox leases, authorization
windows, marker reconciliation, and idempotent updates recover a lost response
without duplicate comments and update removal when source time changes.

Before production, run the Asana provider matrix in `docs/testing-playbook.md`
against a disposable real workspace. Mocked contract tests do not prove the
external OAuth app, provider permissions, revocation, or key rotation.

## Try the desktop agent

From the admin dashboard, invite a user and assign a project. The member signs in,
opens the assigned project, chooses **Connect desktop tracker**, and downloads the
one-time `agent.toml`. If the owner configured a platform download URL, the member
also downloads and opens that packaged tracker. Its first-run window asks for the
downloaded `agent.toml`, validates it, and atomically installs it with private file
permissions under the platform's per-user configuration directory. Re-enrollment
or managed rollout can use:

```bash
Dayfinch-Agent --import-config /path/to/downloaded/agent.toml
```

For development directly from this repository:

```bash
python -m pip install -e ".[agent]"
# Put the downloaded agent.toml in this directory.
dayfinch-agent --config ./agent.toml
```

The default opens Dayfinch in **Not tracking** state. The employee chooses a
project/task and explicitly presses Start; the timer then exposes pause, resume,
stop, work-note, live-clock, and capture-now controls. Opening the standard tracker
never starts surveillance or counted time by itself. For a visible terminal-mode
test, use `--no-tray --start`; the downloaded configuration pins the project that
was used for enrollment. macOS requires Screen Recording and Input Monitoring
permission; Wayland support depends on the compositor's capture portal.

The employee can open **Reminder settings** in the desktop tracker to choose local
days, start/end times (including overnight windows), a repeat interval from 5 to
240 minutes, and either a banner or blocking alert. Preferences and the last-delivery
timestamp are encrypted in the device queue. Reminders appear only while the timer
is stopped; stopping during a reminder window defers the next alert by one interval.

Admins can add an **Automatic desktop tracking** policy in Settings for selected
members. Policies can follow a fixed schedule in each employee's local timezone or
their published shifts, can wait for the first detectable keyboard/mouse activity,
and require an explicit accept/decline response in the desktop app. A manual Stop
suppresses another automatic start for that same schedule window. Dayfinch also
enforces one live timer per employee across their web and desktop devices.

Stopping the agent always closes the open work session, whether it is quit from the
tray, interrupted with Ctrl+C, or terminated by a service manager, logout, or
shutdown. That matters because a session left open blocks the employee from
submitting that period's timesheet.

If the device's enrollment token is revoked from the dashboard, the agent reports
the rejection, stops tracking, and exits with status `5` instead of retrying
invisibly. Enroll the device again to issue a new token.

Check a device before enrollment or diagnose missing permissions with:

```bash
dayfinch-agent --diagnose
dayfinch-agent --capture-test
```

The capture test keeps the image only in memory, verifies it, and immediately
discards it. Run it interactively on each real target device before rollout.

Windows, macOS, and Linux agent tests run in CI. A manual `Package desktop agent`
run produces portable binaries plus native Windows Inno Setup, macOS `.pkg`, and
Linux `.deb` installers. Every output has a SHA-256 checksum and GitHub
build-provenance attestation. Manual runs without credentials intentionally create
test-only unsigned Windows/macOS installers. A `v*` tag whose version matches
`agent.__version__` fails closed unless Windows Authenticode and Apple Developer ID
signing/notarization are fully configured; a successful tag publishes the native
installers, portable artifacts, and an Ed25519-signed `dayfinch-update.json`.
Configure the repository secret `DAYFINCH_UPDATE_SIGNING_KEY` with a base64url,
32-byte Ed25519 seed. Keep that seed in a release-only secret manager; only the
public key produced as `dayfinch-update-public-key.txt` belongs in
`TRACKER_AGENT_UPDATE_PUBLIC_KEY`.

The release workflow expects these certificate values:

- Windows secrets `WINDOWS_CERTIFICATE_PFX` (base64 PKCS#12) and
  `WINDOWS_CERTIFICATE_PASSWORD`, plus repository variable
  `WINDOWS_TIMESTAMP_URL` for the RFC 3161 timestamp service.
- macOS secrets `MACOS_APPLICATION_CERTIFICATE_P12`,
  `MACOS_INSTALLER_CERTIFICATE_P12`, `MACOS_CERTIFICATE_PASSWORD`,
  `MACOS_APPLICATION_IDENTITY`, and `MACOS_INSTALLER_IDENTITY`.
- Apple notarization secrets `MACOS_NOTARY_API_KEY_P8` (base64 App Store Connect
  private key), `MACOS_NOTARY_KEY_ID`, and `MACOS_NOTARY_ISSUER`.

The workflow imports certificates into an ephemeral keychain, enables hardened
runtime, submits the signed package with `notarytool --wait`, staples and validates
the ticket, runs Gatekeeper assessment, and removes temporary credentials. Windows
signing uses SHA-256 for the file and timestamp digest and verifies both the portable
executable and installer with the Authenticode policy. A partial credential set is
always rejected.

Set `TRACKER_AGENT_UPDATE_MANIFEST_URL` to the HTTPS manifest URL. Newly enrolled
`agent.toml` files then pin that public key. The agent verifies the manifest,
platform, semantic version, artifact size, and SHA-256 before atomically staging an
update. It checks on startup in notification mode; operators can also run:

```bash
dayfinch-agent --config ./agent.toml --check-update
dayfinch-agent --config ./agent.toml --download-update
dayfinch-agent --config ./agent.toml --install-update
```

`--install-update` is available only in a packaged executable. It copies the current
binary into private rollback storage, launches a detached copy of the old binary,
waits for the command to exit, rechecks both SHA-256 digests, atomically replaces
the target, and runs the new binary's diagnostics. A failed diagnostic or corrupted
replacement restores and verifies the old binary. The durable JSON recovery plan
records `installed`, `rolled_back`, or `recovery_required` under the agent's update
directory. The updater does not silently stop an active timer.

The signed macOS `.app` deliberately refuses inner-executable replacement because
that would invalidate the bundle seal; install a newer signed/notarized `.pkg`
instead. Portable Windows/Linux builds retain verified atomic replacement and
rollback. A certificate-backed tagged release, signed APT repository for Linux, and
the real-device acceptance matrix remain required before production distribution.
Do not place unsigned manual-run artifacts in `TRACKER_AGENT_*_URL`.

On Wayland, tracked screenshots use the XDG ScreenCast portal and its restricted
PipeWire file descriptor. Dayfinch requests persistent monitor consent, stores the
opaque restore token encrypted in the device queue, replaces it after every use
because tokens are single-use, and extracts one frame through GStreamer. The native
Linux package installs the required PipeWire plugins. The first capture prompts for
the monitor; subsequent captures restore that choice when the compositor grants
persistence. Revoked permission, a disappeared monitor, or a portal that declines
persistence correctly causes another consent prompt—Dayfinch never bypasses the
desktop's decision. Wayland blocks passive global input and foreground-app
inspection; Dayfinch uses the GNOME/KDE session-idle API where available and never
fabricates activity.

## Activity accuracy and consent

Idle deduction: after `idle_timeout_seconds` (default 1800) with no OS session
input, the segment is closed at the last real input—not at minute 30—so the entire
idle stretch is removed. Tracking resumes on the next input. If the desktop exposes
no trustworthy idle API, Dayfinch leaves time unchanged instead of guessing.

Faked activity: extremely regular macros and repeated movement patterns are marked
for review and cannot inflate the derived interaction metric. This is not proof of
misconduct: a modified open-source client or a physical input device cannot be made
tamper-proof on an employee-owned computer. Session time, focus, domain, aggregate
input, screenshots, and anomaly signals are kept separate so an admin can review
context instead of relying on a simplistic activity percentage.

This check is unavailable on Wayland. The same restriction that blocks passive
input monitoring also hides synthetic input, so a jiggler both resets the session
idle timer and produces nothing for the detector to inspect. On those desktops the
screenshots and the focus record remain the only evidence.

Website domain: while tracking, the agent independently journals a host-only sample
about every ten seconds; it does not wait for a screenshot. macOS reads the
foreground browser with Automation permission.
Linux and Windows use the WebExtension in `extensions/chromium` (Chrome/Edge 121+
or Firefox 121+): load it as a temporary/unpacked extension and copy the generated
`website_bridge_token` and port from the downloaded `agent.toml` into the extension
options. Its popup can start, pause, resume, and stop the visible
desktop timer for assigned projects/tasks; controlling that timer enables the same
disclosed desktop collection policy as pressing Start in the desktop window. The
token-secured bridge listens only on `127.0.0.1`, accepts extension origins, and
never exposes the device enrollment token. Both extension and server reduce website
reports to a hostname; paths, searches, titles, and history are discarded. Set
`collect_websites = false` to disable website collection.

Consent is not bypassed. Windows normally has no screen-capture prompt; macOS asks
for Screen Recording once and remembers the grant; Wayland controls consent through
its portal and restores the previous monitor only while the compositor keeps that
permission valid.
The agent also requires `consent_confirmed = true`, prints what it collects, and
keeps visible pause/resume controls.

The server accepts screenshots and app/domain samples only when their timestamp is
covered by an active tracked-time segment. Paused/stopped monitoring data is rejected.
Disabling an account, removing/downgrading project access, or archiving a project
closes open segments and breaks immediately; restoring an account never revives an
old device token.

## Offline and shutdown safety

Every heartbeat and state transition is committed to a local SQLite journal before
network access. UUIDs make replay idempotent. While offline, minute-level events
prove continuous work and screenshots remain in the bounded queue; state events are
uploaded first when connectivity returns so captures resolve to the correct session.
After an abrupt shutdown, a later gap caps the old segment one heartbeat after its
last durable observation. The compact time-state journal survives long outages;
screenshots use the configured bounded queue and discard the oldest capture first
when full. Time after the last durable checkpoint cannot be inferred.

Offline screenshots, application names, domains, and work notes are AES-256-GCM
encrypted using a key derived from the device enrollment secret. State transitions
remain queryable for ordered replay, but their notes are encrypted. Re-enrolling a
device with queued work requires draining or deliberately discarding the old queue
before replacing its enrollment secret.

## Desktop cost

Screenshots are the expensive step, so captures are shrunk by a whole-number
factor to `max_image_dimension` (default 1920) before JPEG encoding. Box reduction
avoids encoding the original 4K/multi-monitor buffer. Set `max_image_dimension = 0`
to keep the original resolution.

Between captures the agent does almost nothing: the pending-upload count is kept in
memory instead of querying its local queue, and the foreground-application check —
the only step that starts a helper process, on X11 and macOS — runs every ten
seconds rather than every five.

Raise `capture_interval_seconds` to lower the cost further; it is the setting with
the largest effect.

## Configuration

`.env.example` contains the local Compose values plus a documented private
S3-compatible storage configuration. Set `TRACKER_STORAGE_BACKEND=s3`, bucket,
region/endpoint, and standard `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`
credentials; objects are private, encrypted with AES-256 by default, version-aware,
and served only after Dayfinch authorization. Production deployments should also
use HTTPS, secure cookies, managed secrets, signed agents, backups, monitoring, and
an independent security/privacy review.
The blocking release criteria are tracked in
[docs/production-readiness.md](docs/production-readiness.md); do not represent a
deployment as production-approved until that register is closed.

Scheduled emails use the `TRACKER_SMTP_*` settings. Payroll supports manual
settlement, an authenticated custom webhook adapter, direct PayPal Payouts, and
direct Wise balance-funded transfers.
For PayPal, set `TRACKER_PAYMENT_PROVIDER=paypal` plus the PayPal client ID,
secret, and sandbox or live API origin. The owner must separately confirm each
member's payout email on People; Dayfinch snapshots that recipient, uses the
payroll UUID as both provider idempotency keys, and reconciles processing payouts
every five minutes. Production mode rejects the PayPal sandbox origin. For Wise,
set `TRACKER_PAYMENT_PROVIDER=wise`, token, profile ID, balance ID, source currency,
and versioned API URL. The owner confirms each member's existing Wise recipient
account ID and target currency. Dayfinch creates an idempotent quote/transfer,
funds it from the configured balance, and reconciles pending transfers every five
minutes. Create Wise schema `4.0.0` subscriptions for
`transfers#state-change`, `transfers#payout-failure`, and `transfers#refund`, all
pointing to `https://<dayfinch-host>/api/v1/payroll/wise-webhook`. Dayfinch verifies
Wise's RSA-SHA256 signature over the raw body, deduplicates delivery IDs, rejects
other profiles, orders events by `data.occurred_at`, and queues a canonical API
refresh. This event path remains effective after the normal 90-day polling window.
The documented Wise sandbox/live public key is selected from the configured API
URL; `TRACKER_WISE_WEBHOOK_PUBLIC_KEY_B64` is an explicit rotation override.
Paid PayPal and Wise records are also checked daily for 90 days; a later provider
refund, return, or chargeback becomes an auditable `reversed` payroll and is
removed from paid totals. Production mode accepts only the versioned Wise live
origin. Wise account approval, balance funding, SCA, webhook subscription setup,
and recipient compliance stay under the operator's control.

Set `TRACKER_DOCUMENT_ENCRYPTION_KEY` to a stable URL-safe base64 encoding of 32
random bytes. Full invoice snapshots are authenticated and encrypted before local
or S3 storage; only authorized server-side decryption renders the printable copy.
Changing this key without re-encrypting existing documents makes them unreadable.
OAuth connectors with persistent credentials use
`TRACKER_INTEGRATION_ENCRYPTION_KEYS`, formatted as a comma-separated keyring with
the primary entry first (`key-id:base64url-32-byte-key`). Add a new primary before
retaining previous entries; a bounded singleton job rewraps stored credentials
online. Remove an old entry only after the production rotation drill confirms no
row still references it. Escrow this keyring separately from the database backup.
Use [docs/testing-playbook.md](docs/testing-playbook.md) for the automated gate,
online/offline A/B comparisons, and manual cross-platform edge-case checklist.

Licensed under the [MIT License](LICENSE).

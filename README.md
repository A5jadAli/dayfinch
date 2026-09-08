# Dayfinch

Dayfinch is a complete, self-hosted workforce operations platform inspired by the
workflows of modern time trackers. It combines a Windows/macOS/Linux desktop timer,
a web timer, activity context, workforce administration, field operations, and
financial workflows under the original Dayfinch brand.

## Current capabilities

- Owner, manager, member, and project-viewer access; invitations, teams and team
  leads; project membership; per-member pay/bill rates and daily/weekly limits.
- Desktop and web timers with project/task switching, work notes, breaks,
  idle-time deduction, durable offline replay, and graceful shutdown recovery.
- Randomized multi-monitor screenshots (0–3 per ten minutes), irreversible
  on-device blur, keyboard/mouse activity levels, independent encrypted app/domain
  sampling, and unusual input signals. Dayfinch never records typed keys.
- Dashboard, screenshot gallery, app/URL summaries, manual-time approvals,
  timesheet submission/review/locking, notifications, and CSV exports.
- Projects, tasks, global/project to-dos, clients, hour/cost budgets, billable
  rates, schedules, attendance, PTO, holidays schema, and expense approvals.
- AES-256-GCM sealed invoice documents with tamper detection, invoice status, pay
  rates, overtime-aware payroll runs, HMAC-signed payment-provider callbacks,
  integrations, and emailed scheduled reports.
- Installable field PWA with AES-GCM offline timer/location journaling, mobile GPS
  API, job-site geofences, automatic timer actions, enter/exit events, and
  scheduled-versus-worked attendance reports.
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
extensions/ Browser integration that reports the active domain only
tests/      API, database, security, storage, agent, and workflow tests
packaging/  Desktop-agent packaging entry point
```

## Run locally with Docker

Requirements: Docker with Compose and ports `8000` and `5432` available.

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
export TRACKER_TEST_DATABASE_URL="postgresql://dayfinch:YOUR_POSTGRES_PASSWORD@127.0.0.1:5432/dayfinch_test"
python -m compileall -q api ui agent tests
python -m pytest
```

If the test database already exists, the first command can be skipped. Use the same
database password you placed in `.env`. Tests truncate only `dayfinch_test` and do
not use SQLite; SQLite is limited to the desktop agent's local retry queue.

## Try the desktop agent

From the admin dashboard, invite a user, assign projects, and create an enrollment
token. Then:

```bash
python -m pip install -e ".[agent]"
cp agent.toml.example agent.toml
# Set the token and explicitly confirm consent in agent.toml.
dayfinch-agent --config ./agent.toml
```

The default opens the Dayfinch timer with project/task selection, work notes, a
live clock, pause/resume, and capture-now controls. Use `--no-tray` for a visible
terminal/service-mode test. macOS requires Screen Recording and Input Monitoring
permission; Wayland support depends on the compositor's capture portal.

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

Windows, macOS, and Linux agent tests run in CI. The `Package desktop agent`
workflow produces unsigned, portable binaries for all three systems when manually
started or when a `v*` tag is pushed. Signing/notarization remains disabled until
the corresponding certificates are stored as repository secrets.

On Wayland, screenshots use the XDG desktop portal and remain subject to its
consent UI. The current Screenshot-portal implementation may prompt for each
capture. A persistent ScreenCast/PipeWire capture session with a rotating restore
token is still required before Dayfinch can promise one prompt per installation.
Wayland blocks passive global input and foreground-app inspection; Dayfinch uses
the GNOME/KDE session-idle API where available and never fabricates activity.

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
or Firefox 121+): load it as a temporary/unpacked extension, generate a random 32+
character token, place the same token in `agent.toml` and the extension options,
then restart the agent. The token-secured
bridge listens only on `127.0.0.1`. Both extension and server reduce reports to a
hostname; paths, searches, titles, and history are discarded. Set
`collect_websites = false` to disable collection.

Consent is not bypassed. Windows normally has no screen-capture prompt; macOS asks
for Screen Recording once and remembers the grant; Wayland controls consent through
its portal and may prompt again until the persistent ScreenCast work above lands.
The agent also requires `consent_confirmed = true`, prints what it collects, and
keeps visible pause/resume controls.

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

Scheduled emails use the `TRACKER_SMTP_*` settings. Payroll dispatch uses an
operator-controlled adapter configured with `TRACKER_PAYMENT_WEBHOOK_URL` and
`TRACKER_PAYMENT_WEBHOOK_SECRET`; this keeps provider credentials outside
Dayfinch while authenticating outgoing requests and status callbacks.

Set `TRACKER_DOCUMENT_ENCRYPTION_KEY` to a stable URL-safe base64 encoding of 32
random bytes. Full invoice snapshots are authenticated and encrypted before local
or S3 storage; only authorized server-side decryption renders the printable copy.
Changing this key without re-encrypting existing documents makes them unreadable.
Use [docs/testing-playbook.md](docs/testing-playbook.md) for the automated gate,
online/offline A/B comparisons, and manual cross-platform edge-case checklist.

Licensed under the [MIT License](LICENSE).

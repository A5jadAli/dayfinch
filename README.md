# Dayfinch

Dayfinch is a transparent, consent-based time and activity tracker for teams using
Windows, macOS, or Linux. It is designed for modern AI-assisted work: activity
includes foreground focus and recent interaction time, so reading code, reviewing
AI output, builds, and debugging are not incorrectly treated as zero work.

## Current capabilities

- Admin invitations, member accounts, and secure one-time device enrollment.
- Users can belong to multiple projects; devices, tasks, sessions, screenshots,
  and timesheets remain attributed to the correct project.
- Visible pause/resume controls, configurable screenshots, aggregate keyboard and
  mouse counts, foreground application names, and a bounded offline queue.
- Foreground website domain (host only), automatic idle deduction after a
  configurable no-input period, and detection of synthetic ("faked") input.
- Work sessions and submitted timesheets with admin approval/rejection, audit
  events, review notes, and approved-period locking.
- PostgreSQL persistence, versioned migrations, private local or S3-compatible
  screenshot storage, retention cleanup, and owner-controlled interval deletion.
- Privacy by design: no key values, clipboard content, full window titles, full page
  URLs, browser history, audio, webcam recording, or user-file collection. Only the
  domain of the active browser tab is recorded, and only what the running agent
  discloses on start-up is collected.

Dayfinch is ready for local evaluation, not yet a complete production replacement
for Hubstaff. The critical next work is timesheet correction workflows, project
budgets and alerts, manager roles, exports, stronger authentication, signed desktop
installers, and load/security testing.

## Structure

```text
api/        FastAPI routes, services, repositories, PostgreSQL, and security
ui/         Server-rendered templates and static assets
agent/      Desktop capture, activity signals, tray controls, and offline queue
extensions/ Browser integration that reports the active domain only
tests/      API, database, security, storage, agent, and workflow tests
packaging/  Desktop-agent packaging entry point
```

## Run locally with Docker

Requirements: Docker with Compose and ports `8000` and `5432` available.

```bash
cp .env.example .env
# Replace all three placeholder values in .env.
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
dayfinch-agent --config ./agent.toml --no-tray
```

Use `--no-tray` only for a visible terminal-based local test. Normal desktop use
should keep the tray controls available. macOS requires Screen Recording and Input
Monitoring permission; Wayland support depends on the compositor's capture portal.

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

Website domain: macOS reads the foreground browser with Automation permission.
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
last durable observation. With the defaults, four offline hours and the final
minute-level checkpoint survive; time after the last checkpoint cannot be inferred.

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

`.env.example` contains only the three values required by the local Compose stack.
Other server settings have safe local defaults in [api/config.py](api/config.py).
Production deployments should use HTTPS, secure cookies, a private S3-compatible
bucket, managed secrets, MFA/SSO, signed agents, backups, monitoring, and an
independent security/privacy review.

Licensed under the [MIT License](LICENSE).

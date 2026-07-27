# Dayfinch MVP

A transparent Windows, macOS, and Linux screenshot/activity tracker with
admin-invited member accounts and private local or S3 screenshot storage. The
default capture interval is 10 minutes.

The agent records screenshots, foreground **application names**, and aggregate
keyboard/mouse activity. It never records typed keys, clipboard contents, full
window titles, browser history, audio, webcam video, or user files. Tracking is
disabled until the device owner explicitly confirms consent in its configuration,
and the running agent remains visible with pause/resume and quit controls.

Read [PLAN.md](PLAN.md) before a pilot. It describes the scope, threat model,
privacy requirements, production architecture, and decisions that require legal
and organizational approval.

## What works in this MVP

- Password-protected admin dashboard and private screenshot delivery.
- Admin-only email invitations with one-time, expiring account links.
- Member accounts limited to their own devices and captures.
- Many-to-many project membership, with every enrolled device and screenshot bound
  to one project workspace.
- Project tasks and automatic work sessions driven by agent active, paused, and
  stopped heartbeats, with pause time excluded from tracked duration.
- Immutable project/task/session attribution on new captures for reliable history.
- Owner-controlled deletion of an image and its complete 10-minute activity row.
- One-time device enrollment tokens; only SHA-256 token hashes are stored.
- Idempotent screenshot upload and automatic 30-day retention cleanup.
- Private S3-compatible storage with exact version-aware object deletion.
- 10-minute configurable screenshots of one monitor or the whole virtual desktop.
- Aggregate key press, mouse click, and mouse movement counts—never key values.
- Separate foreground-focus and recent-interaction time, so reading code or
  reviewing AI/build output is visible without pretending it was keyboard input.
- Versioned, idempotent database migrations and domain-separated API/repository
  modules suitable for continued production development.
- Foreground process/application name without the document or window title.
- Visible tray menu with current status, pause/resume, capture-now, and quit.
- Bounded SQLite offline queue that retries when the server becomes reachable.
- HTTPS required by the agent except when connecting to localhost.

This is a local-pilot build, not yet a full multi-tenant Hubstaff replacement. It
does not yet include payroll, schedules, billing, exports, SSO/MFA, or native
mobile agents. Use
PostgreSQL, private S3-compatible storage, SSO/MFA, an admin audit log, signed
installers, and an external security review before a production rollout.

## Project layout

```text
tracker_server/     FastAPI API, SQLite data layer, storage, admin dashboard
tracker_agent/      Cross-platform capture, counters, queue, tray, HTTP client
tests/              Security, queue, persistence, and ingest regression tests
agent.toml.example  Device-side configuration template
compose.yaml        Local server container
PLAN.md             Product, privacy, security, and rollout plan
```

## 1. Run the server locally

Python 3.11 or later is required. In PowerShell:

```powershell
cd dayfinch
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"

$env:TRACKER_ADMIN_PASSWORD = "use-a-long-random-password"
$env:TRACKER_ADMIN_EMAIL = "admin@your-company.com"
$env:TRACKER_SESSION_SECRET = "use-at-least-32-random-characters"
uvicorn tracker_server.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000` and sign in with `TRACKER_ADMIN_EMAIL` and
`TRACKER_ADMIN_PASSWORD`. The admin can create a one-time invitation for an email
address and privately share the generated link. The app does not send email in
this MVP. After accepting the link and setting a password, that person can enroll
their own device, view only their captures, and delete any full 10-minute interval.

For Docker, create `.env` from `.env.example`, replace both secrets, then run:

```powershell
docker compose up --build
```

The compose setup intentionally publishes only to localhost. Remote agents need a
proper DNS name and HTTPS reverse proxy; do not expose this HTTP development port.

## 2. Run an agent on a pilot device

Install the server and agent dependencies into a separate environment:

```powershell
cd dayfinch
python -m venv .agent-venv
.\.agent-venv\Scripts\Activate.ps1
pip install -e ".[agent]"
Copy-Item agent.toml.example agent.toml
```

Edit `agent.toml`:

1. Set `server_url` to the HTTPS server URL (localhost HTTP is allowed for testing).
2. Paste the one-time device token from the dashboard.
3. Show the disclosure to the device owner and set `consent_confirmed = true` only
   after they explicitly agree.
4. Keep `capture_interval_seconds = 600` for a 10-minute interval.

Start the visible agent:

```powershell
dayfinch-agent --config .\agent.toml
```

On a Linux desktop without a system tray, run it visibly in a terminal:

```bash
dayfinch-agent --config ./agent.toml --no-tray
```

### OS permissions

- **Windows:** normal user permissions are sufficient. Security software may ask
  to approve screenshot and global-input-listener libraries.
- **macOS:** grant Screen Recording for screenshots and Accessibility/Input
  Monitoring for aggregate activity counts. Restart the agent after changing them.
- **Linux:** X11 needs `xdotool` for the foreground app name. Wayland compositors
  commonly block global screenshots/input monitoring; use the desktop portal or a
  managed compositor-specific integration in the production agent. The MVP reports
  `Unknown` if it cannot obtain the app name.

## 3. Run checks

```powershell
.\.venv\Scripts\python.exe -m compileall -q tracker_server tracker_agent
.\.venv\Scripts\python.exe -m pytest
```

## Configuration

### Server environment variables

| Variable | Default | Purpose |
|---|---:|---|
| `TRACKER_ADMIN_PASSWORD` | development-only value | Admin dashboard password |
| `TRACKER_ADMIN_EMAIL` | `admin@example.local` | Bootstrap administrator login email |
| `TRACKER_SESSION_SECRET` | development-only value | Signs admin session cookies |
| `TRACKER_DATA_DIR` | `./runtime` | SQLite DB and private screenshot directory |
| `TRACKER_COOKIE_SECURE` | `false` | Set `true` behind HTTPS |
| `TRACKER_MAX_UPLOAD_MB` | `15` | Maximum screenshot upload |
| `TRACKER_RETENTION_DAYS` | `30` | Automatic screenshot/record retention |
| `TRACKER_INVITATION_HOURS` | `168` | One-time invitation lifetime |
| `TRACKER_STORAGE_BACKEND` | `local` | `local` or `s3` screenshot storage |
| `TRACKER_S3_BUCKET` | empty | Required private bucket when using S3 |
| `TRACKER_S3_PREFIX` | `dayfinch/screenshots` | Object-key prefix |
| `TRACKER_S3_REGION` | `us-east-1` | AWS/S3-compatible region |
| `TRACKER_S3_ENDPOINT_URL` | empty | Optional non-AWS S3 endpoint |
| `TRACKER_S3_SSE` | `AES256` | `AES256`, `aws:kms`, or empty for bucket default |
| `TRACKER_S3_KMS_KEY_ID` | empty | Optional KMS key ID for `aws:kms` |

The server refuses neither development default when run locally, so set both
secrets yourself every time. A production launcher should call
`Settings.validate_for_nonlocal()` and fail closed.

## Private S3 storage

Do not send bucket credentials in chat or put them in `agent.toml`. Only the server
needs them. Install the S3 extra and set environment variables (or preferably use
an instance/task IAM role):

```powershell
pip install -e ".[s3]"
$env:TRACKER_STORAGE_BACKEND = "s3"
$env:TRACKER_S3_BUCKET = "your-private-bucket"
$env:TRACKER_S3_REGION = "us-east-1"
$env:AWS_ACCESS_KEY_ID = "..."
$env:AWS_SECRET_ACCESS_KEY = "..."
```

Keep S3 Block Public Access enabled. The server role needs only `s3:PutObject`,
`s3:GetObject`, `s3:DeleteObject`, and—if versioning is enabled—
`s3:DeleteObjectVersion` under the configured prefix. Screenshot URLs are never
made public or pre-signed; authenticated application routes stream objects to an
authorized admin or the owning member. When S3 returns a version ID, it is stored
with the activity row so user deletion targets that exact object version.

If Object Lock or a retention policy forbids deletion, the application leaves the
database row intact and reports the storage error. Choose bucket retention rules
that match the deletion promise shown to users.

## Invitations and deletion

- Only an administrator can add an email and generate an invitation link.
- Links are single-use, expire after seven days by default, and are not recoverable
  because only their SHA-256 hashes are stored.
- The recipient sets a password of at least 12 characters; passwords use salted
  scrypt hashes.
- A member can view, revoke, and enroll only their own devices. Administrators can
  access every device, including legacy unassigned devices.
- **Delete this interval** removes the screenshot object and then deletes its
  timestamp, app name, and keyboard/mouse totals from the database.

### Agent configuration

The supplied example documents every supported value. The offline queue is capped
at `max_queue_items` (500 by default); oldest queued captures are deleted when the
cap is reached so a long outage cannot grow disk usage without bound.

## Packaging the desktop agent

For a pilot executable, install `.[agent,package]` and run PyInstaller against
`packaging/agent_entry.py`. Build separately on each target OS—PyInstaller does not
cross-compile:

```powershell
pip install -e ".[agent,package]"
pyinstaller --noconfirm --clean --name Dayfinch --windowed --collect-all pystray packaging\agent_entry.py
```

The generated binary still requires a local `agent.toml`. Production installers
must be code-signed/notarized, provision configuration through a secure enrollment
flow, show the disclosure, and register startup only with explicit organizational
and device-owner approval.

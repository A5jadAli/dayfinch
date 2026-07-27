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
- Work sessions and submitted timesheets with admin approval/rejection, audit
  events, review notes, and approved-period locking.
- PostgreSQL persistence, versioned migrations, private local or S3-compatible
  screenshot storage, retention cleanup, and owner-controlled interval deletion.
- Privacy by design: no key values, clipboard content, full window titles, browser
  history, audio, webcam recording, or user-file collection.

Dayfinch is ready for local evaluation, not yet a complete production replacement
for Hubstaff. The critical next work is timesheet correction workflows, project
budgets and alerts, manager roles, exports, stronger authentication, signed desktop
installers, and load/security testing.

## Structure

```text
api/        FastAPI routes, services, repositories, PostgreSQL, and security
ui/         Server-rendered templates and static assets
agent/      Desktop capture, activity signals, tray controls, and offline queue
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

## Configuration

`.env.example` contains only the three values required by the local Compose stack.
Other server settings have safe local defaults in [api/config.py](api/config.py).
Production deployments should use HTTPS, secure cookies, a private S3-compatible
bucket, managed secrets, MFA/SSO, signed agents, backups, monitoring, and an
independent security/privacy review.

Licensed under the [MIT License](LICENSE).

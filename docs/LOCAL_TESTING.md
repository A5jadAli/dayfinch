# Local Dayfinch testing

This guide walks a first-time operator through the complete local invitation and
desktop-tracker journey. Mailpit and MinIO are local test stand-ins only. The
desktop agent is run unsigned from this source checkout; none of these steps is a
production deployment, real-email test, or signed-installer acceptance test.

## 1. Start the local services

Install Docker Desktop or Docker Engine with the Compose plugin. From the
repository root:

```bash
cp .env.example .env
```

Open `.env` and replace at least these values:

- `TRACKER_ADMIN_EMAIL` with the local administrator email you will use.
- `TRACKER_ADMIN_PASSWORD` with a long local password.
- `TRACKER_SESSION_SECRET` with at least 32 random characters.
- `POSTGRES_PASSWORD` with a local database password.
- `TRACKER_DOCUMENT_ENCRYPTION_KEY` and `TRACKER_BACKUP_ENCRYPTION_KEY` with
  separate URL-safe base64-encoded 32-byte keys.

Start PostgreSQL, Dayfinch, Mailpit, and versioned MinIO:

```bash
docker compose -f compose.yaml -f compose.local.yaml up --build -d
docker compose -f compose.yaml -f compose.local.yaml ps
curl -fsS http://127.0.0.1:8000/readyz
```

Expected result: every long-running service is `Up`, `/readyz` returns a JSON
response with `"status":"ready"`, Dayfinch opens at
<http://127.0.0.1:8000>, Mailpit at <http://127.0.0.1:8025>, and the MinIO
console at <http://127.0.0.1:9001>. The first successful start creates the admin
from `TRACKER_ADMIN_EMAIL` and `TRACKER_ADMIN_PASSWORD`.

## 2. Create a project and invite an employee

1. Open Dayfinch and sign in with the admin values from `.env`.
2. On the dashboard, create a project such as `Local tracker test`.
3. Enter an employee email in the invitation form and submit it.
4. Open Mailpit, select the message addressed to that employee, and open its
   one-time acceptance link.
5. Set a password of at least 12 characters. The browser signs in as the new
   employee.
6. Sign out, sign back in as the admin, open the project, and add the employee as
   a Worker.

Expected result: the invitation page says the private link was delivered, Mailpit
contains exactly the test email, the accepted employee appears under People, and
the project Members table shows the employee as an active Worker.

## 3. Enroll a desktop tracker

Sign in as the employee, open the assigned project, enter a device name under
Trackers, keep `Desktop` selected, and choose **Create enrollment**. Download
`agent.toml` immediately.

Expected result: the one-time enrollment page contains the Dayfinch server URL,
the assigned project ID, `consent_confirmed = true`, and a private device token.
The token cannot be displayed again. Do not email or paste this file into chat.

## 4. Run the unsigned agent from source

The following commands are for a development checkout. They do not install a
signed Dayfinch release. Run the agent only in the employee's interactive desktop
session, never as a background system service.

### Windows 11 — not verified here

Open PowerShell in the repository:

```powershell
py -3.12 -m venv .venv
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[agent]"
dayfinch-agent --diagnose
dayfinch-agent --capture-test
dayfinch-agent --import-config "$HOME\Downloads\agent.toml"
dayfinch-agent --config "$env:APPDATA\Dayfinch\agent.toml"
```

Expected result: diagnostics find the capture, input, and tray libraries. Windows
normally does not show a general screen-recording consent prompt; if Windows or
endpoint security presents a capture prompt, approve it only for this interactive
test. The capture test holds one frame in memory and discards it. The imported
configuration is stored at `%APPDATA%\Dayfinch\agent.toml` with per-user access.
The final command opens the visible tracker in **Not tracking** state.

### macOS — not verified here

Open Terminal in the repository:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[agent]'
dayfinch-agent --diagnose
dayfinch-agent --capture-test
dayfinch-agent --import-config "$HOME/Downloads/agent.toml"
dayfinch-agent --config "$HOME/Library/Application Support/Dayfinch/agent.toml"
```

Expected result: macOS asks for Screen Recording when capture is first attempted.
Enable Screen Recording for the Terminal/Python process used for this source test.
Input Monitoring enables aggregate keyboard/mouse counts, and Accessibility
enables foreground-application detection; Dayfinch never stores pressed keys.
macOS may require Terminal to be restarted after a grant. The private config is at
`~/Library/Application Support/Dayfinch/agent.toml`, and the tracker initially
shows **Not tracking**.

### Linux desktop — not verified here

Open a terminal in the graphical desktop session:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[agent]'
dayfinch-agent --diagnose
dayfinch-agent --capture-test
dayfinch-agent --import-config "$HOME/Downloads/agent.toml"
dayfinch-agent --config "${XDG_CONFIG_HOME:-$HOME/.config}/dayfinch/agent.toml"
```

Expected result on X11: diagnostics detect `DISPLAY`; screen capture usually needs
no extra prompt, while `xdotool` improves foreground-app reporting. Expected
result on Wayland: the desktop ScreenCast portal shows its own source/monitor
chooser and consent prompt. Approve only the intended monitor. The compositor may
prompt again after permission or source loss. Wayland intentionally blocks passive
global input and other-application inspection, so aggregate activity and app names
can be unavailable without affecting tracked time. The private config is at
`${XDG_CONFIG_HOME:-$HOME/.config}/dayfinch/agent.toml`.

## 5. Produce and view the first screenshot

In the visible agent, select the assigned project, press **Start**, perform a small
amount of ordinary work, and choose **Capture now**. Never grant capture or input
permission unless the employee has knowingly started this test. Then press
**Stop**.

Sign in to Dayfinch as the admin and open **Activity**.

Expected result: a new card identifies the employee and project, shows aggregate
activity, and opens the captured JPEG. MinIO contains a versioned private object;
the web page reads it through authenticated Dayfinch rather than exposing a public
bucket URL. Opening the web timer alone records time but cannot produce desktop
screenshots or input activity.

## 6. Revoke and re-enroll the tracker

As the admin, open **Devices**, select the test device, and choose **Revoke**.
Attempting another upload with that running agent must return an authorization
failure; after repeated rejection the agent stops instead of tracking invisibly.

To test recovery, sign in as the employee, return to the project, create a new
desktop enrollment, replace the installed config using the matching
`--import-config` command above, and start the agent again.

Expected result: the old device remains revoked, its token cannot upload, and the
new device has a different working token. Re-enrollment never silently restores
the revoked token.

## Troubleshooting

- **`address already in use` for PostgreSQL:** another process owns host port
  5433. Stop that process or deliberately change the host side of the PostgreSQL
  mapping and the test database URL together.
- **Docker image pull ends with `EOF`:** retry `docker compose pull postgres`,
  then rerun the complete `up` command. A partial registry download is not a
  Dayfinch application error.
- **`/readyz` is not ready:** run
  `docker compose -f compose.yaml -f compose.local.yaml logs dayfinch-server postgres`
  and correct the first reported database or configuration error.
- **No invitation in Mailpit:** verify the local override was included and open
  `http://127.0.0.1:8025`. The invitation page retains a copyable one-time link
  and reports SMTP delivery failure without discarding it.
- **MinIO rejects the local key:** the local credentials may have changed while
  its volume was retained. Restore the earlier local-only values or, if its test
  data is disposable, stop the stack and explicitly remove only the Dayfinch
  MinIO test volume before restarting.
- **Agent says configuration is invalid:** download a fresh enrollment, keep the
  file unchanged, and use the OS-specific path above. A token must be at least 32
  characters and `consent_confirmed` must be true.
- **No screenshot appears:** confirm the agent—not the web timer—is active, run
  `dayfinch-agent --diagnose`, then `dayfinch-agent --capture-test`, and address
  the reported Screen Recording, portal, display, or dependency failure.
- **Wayland keeps prompting:** install the desktop's XDG portal and GStreamer
  PipeWire support. The compositor owns the prompt and may require a new grant
  after permission/source loss; Dayfinch does not bypass it.
- **No activity percentage:** Screen Recording and counted time can work even
  when aggregate-input permission is unavailable. On macOS grant Input Monitoring;
  on Wayland this limitation is expected.
- **Revoked agent keeps retrying briefly:** it stops after the bounded consecutive
  401 threshold. Re-enroll to obtain a new token; never re-enable the old secret.

Stop the local stack without deleting test data:

```bash
docker compose -f compose.yaml -f compose.local.yaml down
```

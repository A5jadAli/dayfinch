# Dayfinch production checkpoint

## Summary

Dayfinch is a working, test-covered single-workspace workforce tracker: users can be
invited, accept an account, receive a desktop/mobile enrollment secret, track time,
upload desktop screenshots and activity, review work in the web UI, use OIDC/SAML
SSO and SCIM, synchronize GitHub/Jira/Asana tasks and Slack notifications, export
reports/accounting data, and run direct PayPal/Wise payroll flows. The server,
desktop packaging logic, migrations, security boundaries, backup tooling, metrics,
and load harness build and pass automated tests. It is **not approved for production
yet** because signed artifact/store distribution, real-device capture validation,
real identity/provider acceptance, representative capacity/failover/restore drills,
deployment monitoring, and production credentials are operator-owned gates. The
application is also single-workspace per deployment; a shared multi-organization
SaaS deployment would require tenant keys and isolation throughout the schema.

The unfinished, unwired GitLab checkpoint was removed in this review. No GitLab
route, UI, environment option, migration, repository method, or service remains.

## Verification performed on 2026-09-10

- `docker compose build` — passed.
- `.venv/bin/python -m ruff check api agent tests scripts` — passed.
- `.venv/bin/python -m compileall -q api agent tests` — passed.
- `npm run build:css` — passed.
- `.venv/bin/python -m pip wheel . --no-deps --no-build-isolation -w /tmp/dayfinch-wheel-check` — built `dayfinch-0.6.0`.
- Full suite against a newly created PostgreSQL database with no prior migrations:
  **385 passed in 209.21 seconds**.
- `git diff --check` — passed.

## Verified status of the nine reported items

The status means what the repository proves today. “Actually done” does not waive a
separate real-provider or real-device production acceptance gate.

| # | Reported item | Verified status | Code/test evidence | Remaining gate and ownership |
|---|---|---|---|---|
| 1 | Desktop-agent enrollment form missing from UI | **ACTUALLY DONE** | `ui/templates/project.html` renders the desktop/mobile enrollment form; `POST /devices` in `api/routers/devices.py` checks project assignment, creates a one-time device token, and renders `ui/templates/enrollment.html`; `tests/test_project_page.py` verifies the form and downloaded `agent.toml`. | I can maintain the workflow. A real-device acceptance run needs a target Windows/macOS/Linux device from you. |
| 2 | No downloadable/installable employee tracker workflow | **PARTIAL** | `ui/templates/enrollment.html` shows configured platform downloads and first-run import; `agent/onboarding.py` and `agent/installer.py` validate and privately install enrollment/update data; `scripts/package_windows.py`, `scripts/package_macos.py`, `scripts/package_linux.py`, and `.github/workflows/package-agent.yml` build native artifacts. Package/onboarding/update tests pass. | The workflow exists, but production URLs and certificate-backed released installers do not. You must provide signing/notarization credentials and a release destination; I can wire/test those inputs. |
| 3 | Web timer records time only; screenshots/activity require desktop tracker | **ACTUALLY DONE (intentional design)** | `ui/templates/dashboard.html` and `ui/templates/activity.html` explicitly disclose this; `POST /api/v1/activity` in `api/routers/agent_api.py` accepts activity only from an enrolled desktop device and a covering work segment; `tests/test_activity_ingest.py` verifies enforcement. Browsers cannot silently capture the employee desktop. | No code gap. Employees who require activity evidence must use the desktop tracker; admins can enforce `desktop_only` in Settings. |
| 4 | SSO/SCIM settings stored but authentication not implemented | **ACTUALLY DONE** | OIDC routes `/auth/oidc` and `/auth/oidc/callback`, SAML routes `/auth/saml`, `/auth/saml/acs`, and metadata are in `api/routers/auth.py`; SCIM `/scim/v2` is mounted in `api/main.py`. `api/services/oidc.py`, `api/services/saml.py`, and `api/routers/scim.py` implement validation/lifecycle. `tests/test_oidc.py`, `tests/test_saml.py`, and `tests/test_scim.py` pass. | Disposable real-IdP acceptance and certificate/key rotation still need your IdP tenant and test accounts. |
| 5 | Jira, Asana, GitHub, Slack, and accounting integrations only store configuration | **ACTUALLY DONE for implemented connectors** | `api/main.py` schedules GitHub/Jira/Asana sync, Jira worklogs, Asana comments, and Slack outbox delivery. Services are in `api/services/*_integration.py`; contract/web-flow/retry tests pass in `tests/test_github_integration.py`, `tests/test_jira_integration.py`, `tests/test_asana_integration.py`, and `tests/test_slack_integration.py`. QuickBooks IIF export is in `api/services/quickbooks_iif.py`; direct PayPal/Wise delivery/reconciliation/webhooks are in `api/services/payments.py`. | Real-provider acceptance, permission loss, revocation, and credential rotation require your disposable provider apps/accounts. The entire Hubstaff integration catalogue and direct bank/payroll-vendor catalogue are not implemented. |
| 6 | Invitation links generated but not emailed | **ACTUALLY DONE when SMTP is configured** | `POST /invitations` in `api/routers/auth.py` calls `InvitationDelivery.deliver`; `api/services/invitation_delivery.py` performs STARTTLS/authenticated SMTP delivery and reports failure without losing the one-time link; `tests/test_invitation_delivery.py` passes. | You must provide SMTP host/from address/credentials and approve a sender domain. I can run delivery/bounce acceptance once supplied. |
| 7 | Desktop packages unsigned and not production-distributed | **PARTIAL** | `.github/workflows/package-agent.yml` fails tagged releases closed without Authenticode and Apple Developer ID/notarization secrets, signs/verifies Windows artifacts, signs/notarizes/staples macOS packages, publishes checksums and an Ed25519-signed update manifest. Packaging tests pass. | No certificate-backed tagged release, signed APT repository, CDN/release URL, or real install/update/rollback record exists. You must provide certificates, Apple notarization identity, signing seed, and distribution decision. |
| 8 | Wayland/Linux screenshot prompt limitations | **PARTIAL (platform-controlled)** | `agent/capture.py` uses XDG ScreenCast + restricted PipeWire, requests persistence, rotates an encrypted restore token before frame conversion, and falls back to the portal; `tests/test_capture.py` verifies the protocol contract. | GNOME/KDE/compositor policy controls whether consent persists; Dayfinch cannot bypass it. You must provide real GNOME and KDE Wayland devices/sessions for the matrix in `docs/testing-playbook.md`. |
| 9 | “Live desktop sessions” incorrectly includes web timers | **ACTUALLY DONE** | `ui/templates/dashboard.html` now labels the metric “Active web and desktop sessions,” while the timer card is explicitly “Web timer.” | No remaining code gap. |

## Additional gaps found

| Area | Status | Evidence and consequence | Can I close it alone? |
|---|---|---|---|
| Invite → accept → download → enroll → first screenshot end-to-end | **PARTIAL** | Each boundary has automated coverage (`tests/test_accounts.py`, `test_project_page.py`, `test_agent_onboarding.py`, `test_activity_ingest.py`, and `test_s3_storage.py`), but there is no single real-device production run proving a signed installer and private S3 object through the whole flow. | I can automate more of the server path. Final proof needs your signed artifact, S3 credentials, SMTP, and a real target device. |
| Multi-organization data isolation | **CONFIRMED MISSING for shared SaaS** | `api/migrations.py` creates one `organization_settings` row with `id=1`; business tables have no `organization_id`. Role, project, team, device, report, and activity scope inside that one workspace are covered by `tests/test_authorization_matrix.py`, but two organizations cannot safely share this database. | I can build tenancy, but first you must choose shared SaaS versus one isolated deployment/database per company. Shared tenancy is a large schema/API migration. |
| Authentication and authorization | **ACTUALLY DONE in automated scope; external acceptance pending** | Signed sessions, CSRF inventory, password/TOTP throttling, trusted hosts, CSP/HSTS, device bearer tokens, SSO validation, and project/role policies are implemented in `api/web.py`, `api/middleware.py`, and auth services; authorization/security tests pass. | I can fix code findings. Real IdP rotation/attack acceptance needs your provider tenant. |
| General request rate limiting | **PARTIAL** | Password and TOTP attempts use PostgreSQL-backed throttling across replicas. Uploads have body limits and provider callbacks use signatures/deduplication, but there is no global per-IP/device API rate limiter. | I can add an application limiter, but you should choose the production proxy/API gateway and limits; proxy enforcement is preferable for volumetric abuse. |
| Migrations | **ACTUALLY DONE** | `api/migrations.py` contains sequential migrations 1–60; `tests/test_database.py` verifies the recorded list. The full suite passed from an empty PostgreSQL database during this checkpoint. | No input needed for current schema. Production rollout still needs a pre-deploy backup and rehearsal. |
| Error handling and durable retries | **PARTIAL** | Agent queues, integration claims/outboxes, payment reconciliation, bounded provider responses, idempotency, and readiness failure paths are tested. Live outage behavior has not been exercised against the chosen SMTP/S3/IdP/provider infrastructure. | I can run the drills after you supply disposable infrastructure and expected retry/SLO policy. |
| Logging, metrics, and alerting | **PARTIAL** | `api/observability.py` emits privacy-limited JSON logs, correlation IDs, request/job metrics; `/livez`, `/readyz`, and protected `/metrics` are in `api/main.py`; tests pass. No deployed aggregation, exception sink, dashboards, paging rules, or retention evidence exists. | Needs your monitoring/hosting choice and alert destinations; I can provide/configure dashboards and alerts afterward. |
| Backups and disaster recovery | **PARTIAL** | `scripts/backup_restore.py` creates authenticated encrypted PostgreSQL + exact-version object archives; tests and a small local drill are documented. No scheduled off-site production backup or representative RPO/RTO/failover drill exists. | Needs your backup location, key escrow owner, retention policy, RPO/RTO, PostgreSQL/S3 environment, and maintenance window. |
| Capacity/performance | **PARTIAL** | `scripts/load_test.py` measures real timer/heartbeat/multipart screenshot ingestion and `tests/test_load_test.py` validates its safety contract. No representative 30-minute peak/2×-peak result, dashboard/report load, S3 latency, or failover profile exists. | Needs your launch concurrency, latency/error targets, and production-like environment. I can run and analyze it once supplied. |
| Mobile-store distribution | **PARTIAL** | `mobile/` contains the Expo/React Native tracker and CI/build instructions; code-level validation exists, but no signed App Store/Play build or real-device background/location acceptance has been recorded. | Needs Apple/Google/EAS accounts, signing credentials, privacy disclosures, and physical iOS/Android devices. |
| Integration catalogue breadth | **CONFIRMED MISSING beyond current connectors** | Current production-oriented implementations cover GitHub, Jira, Asana, Slack, QuickBooks IIF, PayPal, and Wise. `docs/integration-catalog.md` records other catalogue gaps. | I can implement selected connectors, but you must prioritize vendors and provide developer/sandbox accounts. This is not one finite “clone everything” release item. |
| Production topology | **PARTIAL** | `compose.yaml` is hardened for a local/single-host deployment, but it is not evidence of managed TLS, HA PostgreSQL, autoscaling, secret management, WAF/rate limits, S3 lifecycle/versioning, or multi-region recovery. | Needs your cloud, region, domain, budget, availability target, and single-tenant/multi-tenant decision. |

## Needs from you

1. Decide whether each company gets an isolated Dayfinch deployment/database or the
   product must become one multi-organization SaaS database.
2. Choose the production cloud/region/domain, TLS/load balancer, managed PostgreSQL,
   deployment platform, monitoring/exception system, availability target, and budget.
3. Provide a private versioned S3-compatible bucket, SSE/KMS decision and scoped
   credentials; provide a separate backup destination and escrowed backup key.
4. Provide SMTP credentials, approved From address/domain, and a disposable mailbox.
5. Provide Windows Authenticode PFX/timestamp policy, Apple Developer ID application
   and installer certificates, App Store Connect notarization key, Ed25519 update
   seed, release repository/CDN choice, and Linux signing/repository decision.
6. Provide Windows 11, current macOS, X11 Linux, GNOME Wayland, and KDE Wayland test
   devices; provide physical iOS and Android devices plus Apple/Google/EAS accounts.
7. Provide disposable OIDC and SAML tenants plus SCIM token/client setup and key/
   certificate rotation access.
8. Provide disposable GitHub, Jira, Asana, Slack, PayPal, Wise, and the supported
   QuickBooks Desktop environment; identify any additional connector required at launch.
9. Set launch concurrency, request latency/error thresholds, RPO/RTO, data retention,
   alert recipients, payroll currencies/countries, and compliance/privacy requirements.

Do not place credentials in Git or chat. Put them in the selected secret manager or
local ignored `.env` only for disposable acceptance work.

## Prioritized plan to production

### P0 — blocks launch

| Work | Rough size | Dependency |
|---|---:|---|
| Decide tenancy model. If shared SaaS is required, add organization ownership to every business row, query, cache/key, background job, integration, object key, audit event, and authorization test. | Decision: hours; shared-tenancy implementation: **6–10 weeks** | Your architecture decision |
| Provision production-like TLS, PostgreSQL, private versioned S3/KMS, secrets, SMTP, monitoring, and backup target; configure Dayfinch invariants. | **1–2 weeks** | Cloud/domain/budget/credentials |
| Produce certificate-backed Windows/macOS/Linux releases, update manifest/channel, and run clean install/update/rollback on the platform matrix. | **1–2 weeks** after credentials | Signing credentials and devices |
| Run the complete invite-to-first-S3-screenshot workflow using the signed release, including revocation and reinstall. | **2–4 days** | SMTP, S3, signed artifacts, devices |
| Run real OIDC/SAML/SCIM acceptance and rotation/revocation tests. | **3–5 days** | Disposable IdP tenants |
| Schedule encrypted off-site backups and pass representative restore, PostgreSQL failover, S3/KMS-denial, disk/queue, and network drills with recorded RPO/RTO. | **4–7 days** | Production-like infrastructure and targets |
| Build signed mobile-store artifacts and pass background location/offline/revocation matrix, if native mobile is a launch requirement. | **1–3 weeks** | Store accounts, disclosures, devices |

### P1 — needed soon after launch

| Work | Rough size | Dependency |
|---|---:|---|
| Run real GitHub/Jira/Asana/Slack and PayPal/Wise/QuickBooks acceptance, lost-response, permission-loss, and key-rotation scenarios. | **1–2 weeks** | Provider sandboxes/apps |
| Run 30-minute peak and 2×-peak capacity tests with dashboard/report traffic, set pool/replica sizes, and publish the capacity envelope. | **3–5 days** | Load targets and production-like stack |
| Configure centralized logs, exception capture, dashboards, alerts, on-call routing, and retention. | **2–4 days** | Monitoring platform and recipients |
| Add or configure general API/device rate limits and validate legitimate offline replay bursts. | **2–4 days** | Gateway choice and thresholds |
| Complete cross-browser/responsive/accessibility and operational runbook acceptance. | **3–5 days** | Supported-browser matrix and operators |

### P2 — later

| Work | Rough size | Dependency |
|---|---:|---|
| Add prioritized third-party catalogue connectors and direct bank/payroll/accounting vendors. | **2–6+ weeks per complex connector** | Vendor priority, API access, contracts |
| Add multi-region recovery and regional data residency if required. | **3–8 weeks** | Compliance/SLA and cloud architecture |
| Expand analytics, anomaly/evaluation datasets, and long-horizon scale tests. | **2–6 weeks** | Product priorities and representative data |

## Exact commands

### Run locally with Docker

```bash
cp .env.example .env
# Edit .env and set at least POSTGRES_PASSWORD and the documented local secrets.
docker compose up --build -d
docker compose ps
# Open http://127.0.0.1:8000
docker compose logs -f dayfinch-server
```

Stop without deleting data:

```bash
docker compose down
```

### Run locally without Docker

Start PostgreSQL 17 separately and create the configured database, then:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[agent,s3,test]'
set -a
. .env
set +a
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

### Full automated release gate

Create `dayfinch_test` once (an “already exists” message is safe to ignore):

```bash
docker compose exec postgres createdb -U dayfinch dayfinch_test
set -a
. .env
set +a
export TRACKER_TEST_DATABASE_URL="postgresql://dayfinch:${POSTGRES_PASSWORD}@127.0.0.1:5433/dayfinch_test"
.venv/bin/python -m ruff check api agent tests scripts
.venv/bin/python -m compileall -q api agent tests
.venv/bin/python -m pytest
npm run build:css
.venv/bin/python -m pip wheel . --no-deps --no-build-isolation -w /tmp/dayfinch-wheel-check
docker compose build
```

The manual OS, provider, S3, backup, outage, browser, and capacity release gates are
in `docs/testing-playbook.md`; the authoritative remaining release register is
`docs/production-readiness.md`.

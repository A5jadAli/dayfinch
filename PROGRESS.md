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
- Full PostgreSQL-backed suite after WI-10: **405 passed, 1 opt-in local E2E
  skipped**.
- `git diff --check` — passed.
- Playwright acceptance matrix in Chromium, Firefox, and WebKit at 1440px and
  390px: **42 passed**. Axe reported no moderate or minor advisory violations.

## WI-9 accessibility advisory register

- **Moderate:** none observed in the 42-case Playwright matrix on 2026-09-10.
- **Minor:** none observed in the 42-case Playwright matrix on 2026-09-10.

## Local hardening work-item status

| Work item | Status | Evidence |
| --- | --- | --- |
| WI-1 Local stand-ins | **DONE** | The opt-in local Compose override verified Mailpit delivery and a non-null-version MinIO object. |
| WI-2 Invite-to-first-screenshot | **DONE** | One opt-in E2E covers invitation through source-agent screenshot plus revocation, re-enrollment, and privacy denial. |
| WI-3 Manual local guide | **DONE** | `docs/LOCAL_TESTING.md` gives the complete local workflow and labels unverified OS steps. |
| WI-4 Rate limiting | **DONE** | PostgreSQL-backed anonymous/web/device/replay limits, exemptions, 429, and Retry-After have regression tests. |
| WI-5 Load baseline | **DONE** | The measured 10-minute, 2,436-request local MinIO baseline is recorded without making a production capacity claim. |
| WI-6 Failure drills | **DONE** | The automated local drill passed SMTP, MinIO, PostgreSQL, and server outage/recovery cases without data loss. |
| WI-7 Backup/restore | **DONE** | The isolated wipe/restore matched 69 tables, 1,860 rows, and 802 referenced objects and includes opt-in scheduling. |
| WI-8 Monitoring | **DONE** | Eight vendor-neutral Prometheus alerts and the Grafana dashboard passed promtool/JSON validation. |
| WI-9 Browser/accessibility | **DONE** | One pinned matrix passed 42 Chromium/Firefox/WebKit desktop/mobile cases with axe and overflow checks. |
| WI-10 Operator handoff | **DONE** | The value-free configuration preflight has six tests, the runbook covers every requested operation/alert, and the operator checklist names exact inputs and commands. |

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
| Invite → accept → download → enroll → first screenshot end-to-end | **LOCAL PATH DONE; PRODUCTION ACCEPTANCE BLOCKED** | `tests/test_local_onboarding_e2e.py` passes the complete local Mailpit/MinIO/source-agent path, revocation/re-enrollment, admin visibility, and cross-employee denial. It deliberately uses a pytest-only frame source and unsigned source agent. | Final proof needs a signed artifact, real SMTP/S3 credentials, and a target Windows/macOS/Linux device. |
| Multi-organization data isolation | **CONFIRMED MISSING for shared SaaS** | `api/migrations.py` creates one `organization_settings` row with `id=1`; business tables have no `organization_id`. Role, project, team, device, report, and activity scope inside that one workspace are covered by `tests/test_authorization_matrix.py`, but two organizations cannot safely share this database. | I can build tenancy, but first you must choose shared SaaS versus one isolated deployment/database per company. Shared tenancy is a large schema/API migration. |
| Authentication and authorization | **ACTUALLY DONE in automated scope; external acceptance pending** | Signed sessions, CSRF inventory, password/TOTP throttling, trusted hosts, CSP/HSTS, device bearer tokens, SSO validation, and project/role policies are implemented in `api/web.py`, `api/middleware.py`, and auth services; authorization/security tests pass. | I can fix code findings. Real IdP rotation/attack acceptance needs your provider tenant. |
| General request rate limiting | **ACTUALLY DONE in application scope** | PostgreSQL fixed-window buckets cover anonymous, signed-in web, ordinary device, and high-volume replay requests consistently across replicas; tests cover 429, Retry-After, overrides, replay, and health exemptions. | A production edge/WAF remains advisable for volumetric attacks, but it is not an application-code gap. |
| Migrations | **ACTUALLY DONE** | `api/migrations.py` contains sequential migrations 1–61; `tests/test_database.py` verifies the recorded list. The full suite passed from an empty PostgreSQL database during this checkpoint. | No input needed for current schema. Production rollout still needs a pre-deploy backup and rehearsal. |
| Error handling and durable retries | **LOCAL DRILLS DONE; PRODUCTION VALIDATION BLOCKED** | Agent queues, integration claims/outboxes, payment reconciliation, idempotency, and readiness paths are tested; `scripts/local_failure_drills.py` passed SMTP, MinIO, PostgreSQL, and server local outages. | Live drills still need the chosen SMTP/S3/IdP/provider infrastructure and approved SLO policy. |
| Logging, metrics, and alerting | **LOCAL STARTER DONE; DEPLOYMENT BLOCKED** | Privacy-limited logs, protected metrics, eight validated Prometheus alerts, a Grafana dashboard, and per-alert first response in `docs/runbook.md` are present. | Central aggregation, exception capture, scraper/dashboard deployment, paging, recipients, and retention need the production monitoring platform. |
| Backups and disaster recovery | **LOCAL DRILL DONE; PRODUCTION SCHEDULING BLOCKED** | Authenticated encrypted PostgreSQL/exact-version object backup, opt-in systemd scheduling, and an isolated 69-table/802-object wipe-and-restore drill passed. | Off-site destination, escrow owner, retention, production schedule, RPO/RTO, and representative failover drill remain operator-owned. |
| Capacity/performance | **LOCAL BASELINE DONE; PRODUCTION ENVELOPE BLOCKED** | A real 10-minute local run included dashboard, timesheet, report, heartbeat, and screenshot traffic: 2,436 requests, 4.059 req/s, zero errors, with p50/p95/p99 recorded. | Peak/2×-peak 30-minute runs need launch concurrency, latency/error targets, and production-like infrastructure. |
| Mobile-store distribution | **PARTIAL** | `mobile/` contains the Expo/React Native tracker and CI/build instructions; code-level validation exists, but no signed App Store/Play build or real-device background/location acceptance has been recorded. | Needs Apple/Google/EAS accounts, signing credentials, privacy disclosures, and physical iOS/Android devices. |
| Integration catalogue breadth | **CONFIRMED MISSING beyond current connectors** | Current production-oriented implementations cover GitHub, Jira, Asana, Slack, QuickBooks IIF, PayPal, and Wise. `docs/integration-catalog.md` records other catalogue gaps. | I can implement selected connectors, but you must prioritize vendors and provide developer/sandbox accounts. This is not one finite “clone everything” release item. |
| Production topology | **PARTIAL** | `compose.yaml` is hardened for a local/single-host deployment, but it is not evidence of managed TLS, HA PostgreSQL, autoscaling, secret management, WAF/rate limits, S3 lifecycle/versioning, or multi-region recovery. | Needs your cloud, region, domain, budget, availability target, and single-tenant/multi-tenant decision. |

## Needs from you

- [ ] **Core deployment:** set `TRACKER_ENVIRONMENT`, `TRACKER_PUBLIC_URL`,
  `TRACKER_ALLOWED_HOSTS`, `TRACKER_COOKIE_SECURE`, `TRACKER_DATABASE_URL`,
  `POSTGRES_PASSWORD`, `TRACKER_DATA_DIR`, `TRACKER_ADMIN_EMAIL`, `TRACKER_ADMIN_PASSWORD`,
  `TRACKER_SESSION_SECRET`, `TRACKER_DOCUMENT_ENCRYPTION_KEY`, and
  `TRACKER_INTEGRATION_ENCRYPTION_KEYS`. Verify format with
  `.venv/bin/python scripts/check_config.py --env-file /etc/dayfinch/production.env --category core`.
- [ ] **Private object storage:** set `TRACKER_STORAGE_BACKEND=s3`,
  `TRACKER_S3_BUCKET`, `TRACKER_S3_REGION`, `TRACKER_S3_SSE`, optional
  `TRACKER_S3_KMS_KEY_ID`/`TRACKER_S3_ENDPOINT_URL`, and scoped
  `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` plus optional `AWS_SESSION_TOKEN`.
  Verify format with `.venv/bin/python scripts/check_config.py --env-file
  /etc/dayfinch/production.env --category s3`; separately prove bucket versioning
  and exact-version read/write/delete with the provider.
- [ ] **Email:** set `TRACKER_SMTP_HOST`, `TRACKER_SMTP_PORT`,
  `TRACKER_SMTP_USERNAME`, `TRACKER_SMTP_PASSWORD`, `TRACKER_SMTP_FROM_EMAIL`,
  `TRACKER_SMTP_STARTTLS`, and `TRACKER_SMTP_TIMEOUT_SECONDS`. Verify format with
  `.venv/bin/python scripts/check_config.py --env-file /etc/dayfinch/production.env --category smtp`.
- [ ] **Identity:** set `TRACKER_OIDC_ISSUER`, optional
  `TRACKER_OIDC_DISCOVERY_URL`/`TRACKER_OIDC_END_SESSION_URL`,
  `TRACKER_OIDC_CLIENT_ID`, `TRACKER_OIDC_CLIENT_SECRET`,
  `TRACKER_OIDC_CLIENT_AUTH_METHOD`, `TRACKER_SAML_IDP_ENTITY_ID`,
  `TRACKER_SAML_IDP_METADATA_B64`, `TRACKER_SAML_SP_PRIVATE_KEY_B64`,
  `TRACKER_SAML_SP_CERTIFICATE_B64`, `TRACKER_SAML_EMAIL_ATTRIBUTE`, and
  `TRACKER_SCIM_BEARER_TOKEN`. Verify format/key pairing with
  `.venv/bin/python scripts/check_config.py --env-file /etc/dayfinch/production.env --category sso`.
- [ ] **Signed desktop release:** set `WINDOWS_CERTIFICATE_PFX`,
  `WINDOWS_CERTIFICATE_PASSWORD`, `WINDOWS_TIMESTAMP_URL`,
  `MACOS_APPLICATION_CERTIFICATE_P12`, `MACOS_INSTALLER_CERTIFICATE_P12`,
  `MACOS_CERTIFICATE_PASSWORD`, `MACOS_APPLICATION_IDENTITY`,
  `MACOS_INSTALLER_IDENTITY`, `MACOS_NOTARY_API_KEY_P8`,
  `MACOS_NOTARY_KEY_ID`, `MACOS_NOTARY_ISSUER`,
  `DAYFINCH_UPDATE_SIGNING_KEY`, `TRACKER_AGENT_UPDATE_PUBLIC_KEY`,
  `TRACKER_AGENT_UPDATE_MANIFEST_URL`, and all three
  `TRACKER_AGENT_{WINDOWS,MACOS,LINUX}_URL` values. Verify local format and
  signing-key pairing inside the trusted release environment with
  `.venv/bin/python scripts/check_config.py --category signing`; never copy these
  private signing inputs into the server environment. Only a tagged CI release and
  real clean-device install proves certificate-backed acceptance.
- [ ] **Provider integrations:** set `TRACKER_GITHUB_APP_SLUG`,
  `TRACKER_GITHUB_CLIENT_ID`, `TRACKER_GITHUB_CLIENT_SECRET`,
  `TRACKER_GITHUB_PRIVATE_KEY_B64`, `TRACKER_GITHUB_WEBHOOK_SECRET`, GitHub URL/
  API-version settings, each Jira/Asana/Slack client ID and client secret plus its
  URL settings, and `TRACKER_PAYMENT_PROVIDER` with the matching
  `TRACKER_PAYMENT_WEBHOOK_*`, `TRACKER_PAYPAL_*`, or `TRACKER_WISE_*` values.
  Verify format with `.venv/bin/python scripts/check_config.py --env-file
  /etc/dayfinch/production.env --category integrations` and then run the real
  disposable-provider matrix in `docs/testing-playbook.md`.
- [ ] **Backups:** set a separately escrowed
  `TRACKER_BACKUP_ENCRYPTION_KEY` and scoped absolute
  `DAYFINCH_BACKUP_DESTINATION`. Verify format with `.venv/bin/python
  scripts/check_config.py --env-file /etc/dayfinch/production.env --category backups`,
  then run a disposable restore and record RPO/RTO.
- [ ] **Monitoring:** set a unique `TRACKER_METRICS_BEARER_TOKEN`. Verify it with
  `.venv/bin/python scripts/check_config.py --env-file /etc/dayfinch/production.env
  --category monitoring`; validate the rules with the `promtool` command in
  `deploy/monitoring/README.md`, import the dashboard, and configure the chosen
  alert recipients.

Do not place credentials in Git or chat. Put them in the selected secret manager or
mode-`0600` production environment file; use a local ignored `.env` only for
disposable acceptance work. These format checks cannot choose the tenancy model,
cloud/region/domain, SLOs, RPO/RTO, retention/compliance policy, alert recipients,
or supported platform/provider catalogue. They also cannot replace acceptance on
real Windows/macOS/Linux/iOS/Android devices and disposable IdP/provider accounts.

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
| Re-run the checked-in browser/accessibility suite and exercise the operational runbook in the production topology. | **1–2 days** | Supported-browser policy, deployment, and operators |

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

## Checkpoint log

- 2026-09-10 — WI-1 — DONE — The opt-in Compose override started Mailpit and a versioned MinIO bucket; a Dayfinch invitation and non-null-version screenshot object were both verified locally.
- 2026-09-10 — WI-2 — DONE — Opt-in local E2E passed invitation delivery/acceptance, project assignment, UI enrollment, source-agent usage and screenshot upload to versioned MinIO, admin visibility, revocation rejection, re-enrollment, and cross-employee denial.
- 2026-09-10 — WI-3 — DONE — The non-expert local guide documents the verified stand-in journey and explicitly marks Windows, macOS, and graphical Linux source-agent steps as not verified here.
- 2026-09-10 — WI-4 — DONE — PostgreSQL-backed anonymous, signed-in web, ordinary-device, and replay-device buckets return 429 with Retry-After across replicas while health checks stay exempt and a tested replay burst remains allowed.
- 2026-09-10 — WI-5 — DONE — A real 600-second, 10-device local MinIO run completed 2,436 mixed ingest/dashboard/timesheet/report requests at 4.059 req/s with 0 errors; per-route percentiles and PostgreSQL observations are recorded as non-production evidence.
- 2026-09-10 — WI-6 — DONE — The automated local drill verified explicit SMTP failure with a retained invitation link, encrypted screenshot and heartbeat replay after MinIO/PostgreSQL/server outages, PostgreSQL readiness 503, and post-recovery admin visibility.
- 2026-09-10 — WI-7 — DONE — An isolated local backup/wipe/restore matched exact inventories for 69 tables, 1,860 rows, and 802 referenced MinIO objects; an opt-in destination plus daily systemd scheduling example is included.
- 2026-09-10 — WI-8 — DONE — Vendor-neutral Prometheus rules and a Grafana dashboard cover readiness, HTTP errors/mean latency, background-job failures, durable queue depths, and screenshot ingest failures, with promtool and JSON validation.
- 2026-09-10 — WI-9 — DONE — One pinned Playwright run passed all 42 login, dashboard, project-enrollment, timesheet, report, screenshot, and settings checks across Chromium, Firefox, and WebKit at desktop/mobile widths, with no axe advisories or layout overflow.
- 2026-09-10 — WI-10 — DONE — The read-only value-free preflight validates eight production configuration categories with six tests, while the runbook and exact-input checklist cover deploy, rollback, migration, rotation, revocation, restore, and all eight monitoring alerts.

# Dayfinch product gap analysis

Updated: 2026-07-27

## Product position

Dayfinch should compete on transparent, low-overhead proof of work for software
teams, not on covert surveillance. Keyboard/mouse totals are weak performance
signals in AI-assisted development, so the product reports foreground focus and
recent interaction separately. Neither metric should be presented as an employee
performance score without output, task, and manager context.

## Capability matrix

| Capability | Status | Production requirement |
|---|---|---|
| Visible desktop capture and offline retry | Implemented | OS-specific signed installers and soak tests |
| Project membership (many-to-many) | Implemented | Manager role and bulk provisioning |
| Project-separated screenshots | Implemented | Project-specific retention/policy controls |
| Reading/AI-wait aware focus measurement | Implemented | Calibrate with pilot data; never call focus “productivity” |
| CSV activity export | Implemented | Date/user filters, background export jobs, signed downloads |
| Security audit events | Foundation implemented | Admin viewer, tamper-evident external sink, IP/user-agent context |
| Tasks and start/stop timer | Not implemented | Required before billing-quality time reports |
| Editable/approvable timesheets | Not implemented | Approval history, lock periods, corrections and comments |
| Budgets, rates, invoices, payroll | Not implemented | Currency/tax/legal design and accounting integrations |
| Scheduling, attendance, breaks, leave | Not implemented | Timezone/DST-safe policy engine and overtime rules |
| App integrations and public API/webhooks | Not implemented | OAuth, scoped tokens, idempotency and rate limits |
| Client/manager roles | Not implemented | Fine-grained RBAC and screenshot visibility policies |
| Screenshot blur/exclusions | Not implemented | On-device redaction; never upload unredacted originals |
| Anomaly/artificial-input detection | Not implemented | Explainable risk flags, appeal workflow, no automatic discipline |
| Mobile/GPS/geofencing | Out of current scope | Separate consent and mobile threat/privacy design |
| SSO/MFA/multi-tenant organizations | Not implemented | Required for enterprise production |

## Performance and quality gates

- Agent target: under 1% average CPU outside capture, bounded memory, and no busy
  polling. Measure p50/p95 capture and upload latency on every supported OS.
- Server target: PostgreSQL before multi-user production, cursor pagination for
  timelines, asynchronous retention/export jobs, and indexed project/date queries.
- Load-test ingest, dashboard, storage failure, retry storms, and retention at the
  expected device count plus 3x headroom.
- Require migration tests, authorization tests for every role/resource pair,
  dependency lock files, static analysis, coverage thresholds, and signed release
  artifacts in CI.
- Establish SLOs, metrics, structured logs, backup/restore drills, and an incident
  response runbook before claiming production readiness.

## Recommended delivery order

1. Tasks, explicit work sessions, automatic timesheets, and daily/project reports.
2. Manager role, approvals, corrections, audit viewer, and project policies.
3. Budgets/rates plus integrations and API/webhooks.
4. Schedules/attendance/leave, anomaly flags, and workload/wellbeing insights.
5. Enterprise tenancy, SSO/MFA, PostgreSQL, observability, and compliance hardening.

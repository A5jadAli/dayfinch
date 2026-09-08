# Dayfinch workforce-tracker parity map

Research refreshed on 2026-09-08 from Hubstaff's public feature and support
documentation. Dayfinch reproduces the operational concepts under its own name,
visual system, source code, and privacy model; it does not copy Hubstaff branding.

## Implemented workflows

| Area | Dayfinch implementation |
| --- | --- |
| Timers | Visible desktop timer, browser timer, project/task switching, notes, pause/break/resume, idle deduction, durable offline replay |
| Activity | Randomized 0–3 screenshots per ten minutes, multi-monitor capture, on-device irreversible blur, activity percentage, independent encrypted app/domain timeline, anomaly flagging |
| Administration | Owner/manager/member/viewer roles, invitations, account disabling, teams/leads, project assignment, rates, opt-in daily/weekly limits, tracking policies |
| Work management | Projects, tasks, archive/restore, billability, clients, hour/cost budgets, global/project to-dos |
| Time review | Tracked and manual entries, approval/rejection, submitted timesheets, review notes, approved-period locking |
| Workforce | Shifts, time off, approval notifications, installable field PWA with encrypted offline timer/GPS replay, geofences, attendance comparison |
| Financials | Expenses and approvals, AES-256-GCM sealed invoices/status, overtime-aware payroll runs, signed payment-provider dispatch, project rates and costing primitives |
| Analytics | Dashboard, screenshot/app/URL views, time/activity/attendance/expense CSVs, scheduled reports with SMTP delivery |
| Platform | PostgreSQL migrations, audit log, encrypted offline queue, S3-compatible private screenshots/documents, retention, TOTP 2FA enforcement, integrations/webhooks |

## Source behavior used in the design

- [Hubstaff features](https://hubstaff.com/features): time tracking, automated
  timesheets, projects/tasks, attendance/PTO, productivity context, payments,
  budgets, invoicing and overtime.
- [Activity overview](https://support.hubstaff.com/activity-tracking-overview/):
  screenshot, app and URL views, activity-level context and filters.
- [Screenshot tracker](https://hubstaff.com/time-tracker-with-screenshots):
  multi-monitor captures and a configurable maximum of three screenshots per ten
  minutes.
- [Screenshot blur](https://support.hubstaff.com/screenshot-blur/): blur is applied
  on the device before upload and cannot be reversed.
- [Roles and permissions](https://support.hubstaff.com/hubstaff-user-rolepermissions-guide/):
  organization/project roles, team leads, project-scoped viewers and manager
  capabilities.
- [Organization settings](https://support.hubstaff.com/organization-settings/):
  company, login, activity, time-edit and approval policy groups.
- [Manual-time approvals](https://support.hubstaff.com/manual-time-approvals/):
  pending additions reviewed before payable timesheets.
- [Timesheet approvals](https://support.hubstaff.com/delaying-payroll-payments-timesheet-approvals/):
  pay-period review, overtime, PTO, amounts owed and payroll gating.
- [Teams](https://support.hubstaff.com/teams-overview/): team leads and delegated
  schedule/timesheet/time-off management.
- [Job sites](https://support.hubstaff.com/job-sites/): geofences, enter/exit events
  and automatic timer-action fields.
- [Tracker applications](https://support.hubstaff.com/time-tracker-apps-overview/):
  desktop project/task timer, activity, work notes, idle time and reminders.

## Platform boundaries

- The installable field PWA and `POST /api/v1/location` provide browser-based GPS
  tracking. Separately signed iOS or Android store packages are not bundled.
- Wayland deliberately uses the desktop consent portal. Compositor restrictions
  may prevent passive input/app observation and may show a capture prompt.
- SSO/SCIM provider metadata is deployment-specific. Dayfinch persists the chosen
  provider/domain and integration records, while identity-provider credentials
  belong in deployment secrets rather than the database or web form.
- Payroll can be dispatched to an operator-controlled, HMAC-signed provider adapter;
  signed callbacks reconcile processing, paid, and failed states idempotently.
  Wise/PayPal/bank accounts and compliance approval remain operator-owned.
- Scheduled reports are generated and emailed when SMTP deployment secrets are set.

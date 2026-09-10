# Dayfinch workforce-tracker parity map

Research refreshed on 2026-09-08 from Hubstaff's public feature and support
documentation. Dayfinch reproduces the operational concepts under its own name,
visual system, source code, and privacy model; it does not copy Hubstaff branding.

The provider-by-provider integration scope and its evidence rule live in
[`integration-catalog.md`](integration-catalog.md).

## Verified implementation status

| Area | Status | Evidence and remaining gap |
| --- | --- | --- |
| Timers | Partial | Desktop, web, native iOS/Android, and authenticated Chromium companion timers support explicit start/pause/resume/stop, project/task selection, and idempotent offline replay. The native client uses a bounded SQLCipher queue, Keychain/Keystore enrollment secret, network/app-resume synchronization, and timer reminders. Desktop additionally supports notes, breaks, idle deduction, crash closure, screenshots, and activity. Organization defaults and individual overrides can require desktop-only tracking; web/field/mobile starts and resumes are denied, geofence auto-start is skipped, and existing disallowed virtual-device sessions are atomically closed while explicit `all` overrides remain active. The extension controls the visible local agent through a token-secured loopback API without receiving the device credential. Consented automatic fixed-schedule/published-shift policies use the member's local timezone, support overnight windows and optional first-activity gating, and durably remember a manual stop across app restarts for the remainder of that window. Employee-configured desktop reminders support selected local days, daytime or overnight windows, five-minute-minimum recurrence, notification/alert presentation, and encrypted restart-safe settings. One employee can have only one live timer across devices. Opening the standard desktop tracker does not count time or collect activity. Web/mobile time intentionally has no desktop activity. Real mobile-store acceptance remains. |
| Activity | Partial | Desktop screenshots, multi-monitor capture, irreversible blur, aggregate input, app/domain samples, anomaly context, and permission-scoped streaming screenshot archives work. Owners/managers can apply inheritable per-member screenshot-frequency, blur, app/domain, idle-timeout, and deletion overrides; the effective policy is enforced by desktop configuration and server ingestion. Wayland uses persistent XDG ScreenCast sessions, restricted PipeWire descriptors, single-frame GStreamer conversion, and encrypted rotating single-use restore tokens. Real GNOME/KDE compositor acceptance and unavoidable re-consent after permission/source loss remain. |
| Administration | Implemented core | Owner/organization-manager/member, project-manager/project-viewer, privacy-limited Manage-IT, and team-lead delegation have route-wide CSRF/authentication guards plus cross-scope denial tests. Tracking eligibility is rechecked server-side and active sessions close on access loss. |
| Work management | Partial | Projects, tasks, billability, budgets, clients, and to-dos work. A least-privilege GitHub App imports mapped repository issues as read-only tasks with OAuth/PKCE installation ownership verification, short-lived installation tokens, pagination, signed/idempotent webhooks, bounded retries, overlapping incremental sync, daily full reconciliation, and access-loss cleanup. Jira Cloud OAuth imports mapped-project issues with encrypted rotating credentials, bounded enhanced-JQL pagination, overlapping incremental sync, two-pass eventual-consistency-safe daily reconciliation, access-loss cleanup, and replica-safe claims. Per-member Jira grants write UTC-day worklogs through a durable outbox. Asana OAuth selects one workspace, imports mapped-project tasks and nested subtasks in bounded leased batches, scopes timer visibility by the connected assignee, reconciles disappearance twice, and delivers each member's tracked time as marker-owned UTC-day comments in off/hourly/daily/delayed/completed modes. Asana authorization windows exclude disconnected time; lost responses reconcile without duplicate comments; source changes update or mark owned comments removed. Disconnecting GitHub, Jira, or Asana preserves imported tasks as editable ordinary tasks while retaining private origin metadata for safe reattachment. Real-provider acceptance remains. |
| Time review | Implemented core | Manual-time review, timesheet submission, notes, approval, and approved-period locking are covered by database-backed tests. |
| Workforce | Partial | Shifts, PTO, field PWA, offline GPS replay, geofences, and attendance comparison work. The native iOS/Android client adds explicit foreground/background location consent, active-timer-only collection, encrypted offline replay, Android foreground-service disclosure, and iOS background indicator configuration. Signed store distribution and real-device background-throttling acceptance remain. |
| Financials | Partial | Expenses, sealed client invoices, member-created team invoices with manual/tracked-time lines, draft editing/submission, scoped management, partial payment recording, CSV export, approved-timesheet and configurable weekly-overtime payroll calculations, a signed generic payment adapter, direct idempotent PayPal Payouts, and balance-funded Wise transfers work. Both direct connectors snapshot owner-confirmed destinations and reconcile later refunds/chargebacks into immutable reversed records. Wise state-change, payout-failure, and refund webhooks are RSA-authenticated, delivery-deduplicated, event-ordered, profile-scoped, and revive canonical reconciliation after the polling window. Direct bank/accounting/payroll-vendor connectors do not. |
| Analytics | Implemented core | Dashboard, activity/app/domain views, permission-scoped custom time reports with saved filters, selectable columns, expanded or collapsed grouping by date/member/member+date/project/client/task, formula-safe CSV, bounded PDF export, and calendar-correct daily/weekly/monthly SMTP delivery in CSV or PDF work. Time/activity, activity-level, attendance, expense, and administrative audit-log reports have matching bounded CSV/PDF exports. The audit log filters author, time, action, object, and affected member with indexed, configurable retention. Oversize output fails visibly and every download is audited. Scheduled exports use selectable completed UTC periods, bounded queries/output and atomically claimed batches across replicas, per-schedule failure isolation, visible error state, and capped exponential retry. QuickBooks Desktop Timer Activity IIF export has persistent list mappings, approved-timesheet gating, duration constraints, and audit totals. Bounded screenshot ZIP archives include missing-object manifests. Group totals use duration-weighted activity rather than an unweighted average. |
| Identity/platform | Partial | PostgreSQL migrations, audit events, encrypted queues, private local/S3 storage, retention, TOTP, distributed login throttling, OIDC SSO, strict SP-initiated SAML with signed requests/responses/assertions and replica-safe replay prevention, SCIM user/team lifecycle, an authenticated desktop update channel, and a rotation-aware AES-GCM OAuth credential vault work. Slack OAuth v2 notifications implement selected channel/person destinations, organization defaults, per-member overrides, timer start/stop and to-do completion events, encrypted rotating tokens, transactional outbox delivery, idempotency IDs, backoff, retention, replica-safe claims, and revocation. Direct accounting adapters and production identity/provider/key-rotation acceptance remain. |

The status in this file is based on reachable routes, service behavior, and tests—not
the presence of a settings field or database table. A provider name in the UI is
not counted as an integration until authentication, synchronization, retries,
idempotency, revocation, and provider-specific tests exist.

## Source behavior used in the design

- [Hubstaff features](https://hubstaff.com/features): time tracking, automated
  timesheets, projects/tasks, attendance/PTO, productivity context, payments,
  budgets, invoicing and overtime.
- [Activity overview](https://support.hubstaff.com/activity-tracking-overview/):
  screenshot, app and URL views, activity-level context and filters.
- [Screenshot tracker](https://hubstaff.com/time-tracker-with-screenshots):
  multi-monitor captures and a configurable maximum of three screenshots per ten
  minutes.
- [Organization settings](https://support.hubstaff.com/organization-settings/):
  organization defaults and individual-member controls for activity collection,
  allowed timer apps, idle timeout, screenshot frequency, blur, and deletion.
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
- [Tracking reminders](https://support.hubstaff.com/reminder-track-time-start-timer-desktop-application/):
  employee-selected local days/hours, recurring notifications or alerts, and a
  five-minute minimum reminder interval while the timer is stopped.
- [Automatic tracking policies](https://support.hubstaff.com/what-is-automatic-start/):
  consented desktop start/stop using fixed member-local schedules or published
  shifts, an optional first-activity gate, and a starting project.
- [Saved report filters](https://support.hubstaff.com/save-custom-filters-reports/):
  reusable date/project/member filters for common report workflows.
- [Reports overview](https://support.hubstaff.com/reports-overview/): selectable
  columns, export, scheduling, and role-scoped report access.
- [Audit log report](https://support.hubstaff.com/audit-log-report/): author, time,
  action, object, affected-member and detail columns with filters and CSV/PDF export.
- [Intuit IIF overview and import kit](https://quickbooks.intuit.com/learn-support/en-us/help-article/list-management/iif-overview-import-kit-sample-files-headers/L5CZIpJne_US_en_US):
  IIF is tab-separated ASCII, header placement is strict, imports have limited
  validation, and Intuit's Timer Activity sample defines TIMERHDR/TIMEACT fields.
- [Team invoices](https://support.hubstaff.com/team-invoices/): member drafts,
  tracked-time line generation, management review, payment recording, and export.
- [Atlassian Jira Cloud worklog API](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-worklogs/):
  per-user create/update/delete permissions, bounded worklog pagination, classic
  read/write scopes, timestamps, and worklog entity properties used for durable
  delivery reconciliation.
- [Asana OAuth](https://developers.asana.com/docs/oauth) and
  [OAuth scopes](https://developers.asana.com/docs/oauth-scopes): S256 PKCE,
  rotating refresh tokens, token introspection, workspace/project/task scopes,
  and per-member grants.
- [Asana pagination](https://developers.asana.com/docs/pagination),
  [rate limits](https://developers.asana.com/docs/rate-limits), and
  [task stories](https://developers.asana.com/reference/createstoryfortask):
  bounded offset traversal, `Retry-After`, comment creation, and idempotent story
  updates used by the durable delivery reconciler.
- [Hubstaff Slack integration](https://support.hubstaff.com/slack-integration-setup/):
  owners select channels or users and configure organization/per-user timer and
  completed to-do notification rules.
- [Slack OAuth v2](https://api.slack.com/authentication/oauth-v2),
  [token rotation](https://api.slack.com/authentication/rotation), and
  [chat.postMessage](https://docs.slack.dev/reference/methods/chat.postmessage):
  bot scopes, rotating credentials, retry behavior, and stable client message IDs.

## Platform boundaries

- The installable field PWA and native Expo/React Native client use
  `POST /api/v1/location`. Native enrollment is issued beside desktop enrollment;
  secrets use Keychain/Keystore and queued locations use SQLCipher. The OS task is
  active only for a consented running timer. Separately signed App Store/Play Store
  packages require operator-owned accounts and are not committed to source control.
- Wayland deliberately uses the desktop consent portal. Persistent ScreenCast
  restore tokens avoid repeated source selection while the compositor keeps its
  grant; revocation or source loss prompts again. Compositor restrictions still
  prevent passive input/app observation.
- OIDC SSO validates signed ID tokens through provider discovery and links only an
  existing active account with a verified email in the configured domain. SCIM
  supports user discovery, provisioning, replacement, PATCH, and deactivation;
  Group/team synchronization is implemented. SAML accepts only SP-initiated,
  request-correlated, short-lived signed responses and assertions from pinned local
  IdP metadata; PostgreSQL response/assertion IDs prevent cross-replica replay.
- Payroll can be dispatched to an operator-controlled, HMAC-signed provider adapter,
  PayPal Payouts, or Wise balance-funded transfers. Direct connectors reconcile
  processing and paid transfers plus bounded post-payment reversals. Provider
  accounts, balances, SCA/compliance approval, and bank rails remain operator-owned.
- Scheduled reports are generated and emailed when SMTP deployment secrets are set.
- GitHub synchronization stores no user or installation access token. The app's
  private key, client secret, and webhook secret remain deployment-managed; a real
  GitHub organization acceptance run and key-rotation drill are still required.
- Asana site/member access and rotating refresh tokens are encrypted with
  account-bound authenticated data. Contract tests cover OAuth/PKCE, pagination,
  rate limits, nested tasks, assignee scope, authorization gaps, lost responses,
  duplicates, and replica leases; a real workspace acceptance and key-rotation
  drill are still required.
- Slack bot access and rotating refresh tokens are encrypted with account-bound
  authenticated data. Contract tests cover state, scopes, pagination, permission
  loss, rate limits, lost responses, per-member rules, replica leases, retention,
  and revocation; a real Slack workspace acceptance and key-rotation drill remain.

## Confirmed production blockers

- Run the implemented fail-closed Windows Authenticode and macOS Developer ID
  signing/notarization pipeline with production certificates, publish a signed APT
  repository for the Linux `.deb`, and acceptance-test first-run import,
  digest-pinned updates, diagnostics, package-level macOS upgrades, and automatic
  Windows/Linux rollback on real devices.
- Acceptance-test the implemented SAML flow against the chosen disposable identity
  provider, including SP metadata import, request/assertion signatures, NameID and
  email mapping, clock skew, replay, deactivation, certificate overlap/expiry, and
  rotation. Repeat OIDC and SCIM acceptance and key-rotation drills.
- Acceptance-test the GitHub App against a disposable real organization, including
  repository selection changes, rate limiting, webhook redelivery, suspension,
  deletion, and private-key/webhook-secret rotation. Acceptance-test Jira Cloud
  OAuth, rotating-token reuse, resource-restricted grants, pagination, access loss,
  worklog permissions/revocation, lost-response recovery, and credential-key
  rotation. Acceptance-test Asana OAuth scopes, multi-workspace selection, nested
  tasks, assignee changes, comments, revocation, pagination/rate limits, and key
  rotation. Acceptance-test Slack channel membership, direct messages, token
  rotation, rate limits, lost responses, app removal, and credential-key rotation.
  Direct accounting/payment connectors remain.
- Maintain the completed route-wide authorization matrix as new endpoints are added.
  Team-lead approvals, scheduling, explicitly assigned projects/members/expenses,
  project manager/viewer, owner-only payroll/profile controls, privacy-limited
  Manage-IT, and screenshot-export boundaries have cross-scope tests.
- Validate the manual OS/browser/device matrix in `testing-playbook.md` on the
  exact platforms being deployed.
- Initialize the operator-owned Apple/Google/EAS projects, complete privacy and
  background-location declarations, build signed iOS/Android store artifacts, and
  pass the mobile device matrix. Source-level Expo Doctor/export/native-generation
  checks and Android compilation remain CI gates but do not replace store review.

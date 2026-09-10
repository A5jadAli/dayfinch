# Hubstaff integration parity register

Last audited: 2026-09-10.

This register turns “the entire Hubstaff integration catalogue” into explicit,
testable scope. The authoritative baseline is Hubstaff's current
[Integrations Overview](https://support.hubstaff.com/integrations-overview/) and
[public catalogue](https://hubstaff.com/integrations). Hubstaff describes the
common contract as provider authentication, project mapping, user mapping,
assigned task import, optional task completion, and Off/Hourly/Daily/Delayed/
Completed time write-back. A connector is not complete here merely because it
stores configuration: it needs that provider's applicable contract tests and a
recorded disposable-account acceptance run.

## Current native catalogue

| Category | Provider | Dayfinch state | Required evidence before parity |
|---|---|---|---|
| Project management | Hubstaff Tasks | Native equivalent | Dayfinch projects/tasks/to-dos and timer contract; no external connector |
| Project management | ActiveCollab | Missing | OAuth/API, project/user mapping, assigned tasks, completion and time write-back |
| Project management | Asana | Implemented; acceptance pending | Disposable workspace scopes, mapping, nested task import, user attribution, comment write-back, revocation/rotation |
| Project management | Breeze | Missing | Authentication, project/user mapping, assigned tasks and time write-back |
| Project management | ClickUp | Missing | OAuth, workspace/project/user mapping, assigned tasks, completion/time write-back |
| Project management | GitHub | Implemented; acceptance pending | Disposable organization installation, repository changes, issue lifecycle, webhooks, suspension and key rotation |
| Project management | GitLab | Missing | OAuth, group/project/user mapping, issues, completion/time write-back and webhooks |
| Project management | Insightly | Missing | OAuth, project/user mapping, assigned tasks and time write-back |
| Project management | Jira | Implemented; acceptance pending | Disposable Cloud site OAuth, issue import, member authorization, worklog modes, access loss and rotation |
| Project management | Monday.com | Missing | OAuth, board/user mapping, assigned items, completion and time write-back |
| Project management | Podio | Missing | OAuth, workspace/app/user mapping, tasks and time write-back |
| Project management | Redbooth | Missing | Authentication, project/user mapping, assigned tasks and time write-back |
| Project management | Redmine | Missing | API-key/OAuth policy, project/user mapping, issues and time-entry write-back |
| Project management | Teamwork Projects | Missing | OAuth, project/user mapping, tasks, completion and time write-back |
| Project management | Trello | Missing | OAuth, board/member mapping, cards, completion and time write-back |
| Project management | Zoho Projects | Missing | OAuth, portal/project/user mapping, tasks and time write-back |
| Communication | Slack | Implemented; acceptance pending | Disposable workspace OAuth, channels/DMs, timer/to-do events, removal, rate limits and rotation |
| Payments/accounting | Bitwage | Missing | Provider onboarding, recipient mapping, idempotent payment and reconciliation |
| Payments/accounting | FreshBooks | Missing | OAuth, project/task/client mapping, time write-back and invoice/accounting reconciliation |
| Payments/accounting | Payoneer | Missing | Approved provider access, recipient mapping, idempotent payment and reversal reconciliation |
| Payments/accounting | PayPal | Implemented; acceptance pending | Sandbox/live payout, duplicates, recipient failures, returns/refunds/reversals and credential rotation |
| Payments/accounting | QuickBooks Desktop | Implemented export; acceptance pending | Exact-edition IIF import and employee/customer/service/class/time reconciliation |
| Payments/accounting | QuickBooks Online | Missing | OAuth, company/employee/customer/service mapping, time export and token rotation |
| Payments/accounting | Wise | Implemented; acceptance pending | Sandbox/live quote, transfer, balance funding, SCA/approval, lost response, RSA-signed state/failure/refund webhook delivery and replay/order checks, bounce/refund/chargeback, token and public-key rotation |
| Payroll/HRIS | Gusto | Missing | Partner/API approval, employee mapping, approved-hours export and payroll reconciliation |
| Payroll/HRIS | Deel | Missing | Partner/API approval, contractor mapping, approved-hours/payment reconciliation |
| Payroll/HRIS | Remote | Missing | Partner/API approval, employee mapping, approved-hours/payment reconciliation |
| CRM | Salesforce | Missing | OAuth, organization/user/project mapping, assigned tasks and time write-back |
| Help desk | Freshdesk | Missing | OAuth/API key, account/agent mapping, assigned tickets and time write-back |
| Help desk | Zendesk | Missing | OAuth, account/agent mapping, assigned tickets and time write-back |
| HR/workforce | BambooHR | Missing | Approved API access, employee lifecycle, PTO policy/request/balance sync and conflict reconciliation |

## Catalogue discrepancies and add-ons

The public marketing catalogue additionally renders LiquidPlanner, Mavenlink,
Paymo, and Unfuddle, while the support page's “currently active” list omits them.
Hubstaff's feature page also advertises Google Calendar, and the catalogue offers
Zapier as a route to hundreds of non-native apps. These are tracked as
provider-confirmation items rather than silently excluded: production parity
requires either a working connector/automation contract or dated evidence from
Hubstaff that the listing is no longer available to new customers.

| Listing | Dayfinch state | Closure evidence |
|---|---|---|
| LiquidPlanner | Confirmation required | New-account availability check, then full connector or removal evidence |
| Mavenlink/Kantata | Confirmation required | New-account availability check, then full connector or removal evidence |
| Paymo | Confirmation required | New-account availability check, then full connector or removal evidence |
| Unfuddle | Confirmation required | New-account availability check, then full connector or removal evidence |
| Google Calendar | Missing | Calendar authorization, scoped event/time behavior and acceptance contract |
| Zapier | Missing | Published authenticated triggers/actions, replay/idempotency tests and live Zap acceptance |

## Completion rule

“Catalogue parity” is closed only when every row is either verified working with
provider acceptance evidence or has dated authoritative evidence that it is not
available in the comparison product. Mock HTTP tests prove Dayfinch's contract and
failure handling, but never substitute for vendor credentials, partner approval,
permission/revocation cases, rate limits, and real API behavior.

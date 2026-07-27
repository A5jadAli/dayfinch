# Pilot checklist

Do not begin a pilot until every applicable item is complete.

## Governance

- Legal/privacy review covers each employee location and the chosen retention.
- Written disclosure names every collected field and who may access it.
- Consent or another valid legal basis is documented before installation.
- Work schedules, pause expectations, support, complaints, and deletion requests
  have named owners.

## Infrastructure

- Server is behind HTTPS; HTTP is not reachable from employee networks.
- Admin and session secrets are random and stored outside source control.
- Screenshot storage is private, backed up as approved, and retention-tested.
- S3 Block Public Access and least-privilege prefix permissions are verified.
- Object versioning/Object Lock behavior matches the user-facing deletion promise.
- Only named administrators can reach the dashboard.
- Restore and incident-response procedures have been exercised.

## Device acceptance

- Tray/status indicator is continuously visible.
- Pause/resume and quit behave as disclosed.
- OS screenshot and input permissions are granted knowingly.
- Sensitive apps/screens can be excluded before broad rollout (production gap).
- CPU, memory, disk, network use, sleep/wake, multiple monitors, and offline retry
  are tested on each supported OS version.

## Exit criteria

- Revoke the device in the dashboard.
- Remove the agent/startup entry from the device.
- Delete its local queue and server data according to the approved policy.
- Record the pilot result and all privacy/security issues before expanding scope.

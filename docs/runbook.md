# Dayfinch operations runbook

This runbook covers the single-workspace deployment implemented in this repository.
Commands assume a reviewed deployment override, a mode-`0600` production environment
file, a private PostgreSQL service, and a private versioned S3-compatible bucket.
Local Mailpit, MinIO, test keys, and unsigned packages are not production services.

## Safety rules

- Never put credentials in Git, command output, tickets, or chat. Load them from the
  deployment secret manager or a protected environment file.
- Take an authenticated encrypted backup before every deployment or migration. A
  database-only snapshot is insufficient because database rows reference exact S3
  object versions.
- Stop all application writers before `dayfinch-ops backup` or `restore` and keep
  them stopped until the command completes.
- Never restore into an unconfirmed database or a bucket/prefix shared with another
  workspace. The restore command is destructive and requires the exact database
  name through `--confirm-database`.
- Never rotate `TRACKER_DOCUMENT_ENCRYPTION_KEY` without a tested document
  re-encryption tool. None is implemented in this repository.

## Preflight

From the exact release checkout, validate deployment presence and format without
contacting providers or printing values:

```bash
.venv/bin/python scripts/check_config.py --env-file /etc/dayfinch/production.env \
  --category core --category smtp --category s3 --category sso \
  --category integrations --category backups --category monitoring
```

The command must finish with `Result: READY`. Validate the `signing` category only
inside the trusted release environment where GitHub Actions/release secrets are
injected; never copy signing private keys into the production server environment.
Running without `--category` intentionally inventories both deployment and release
inputs. These checks prove local syntax and key pairing only; they do not prove
credentials, certificates, provider permissions, bucket versioning, alert routing,
or real-device behavior.

Set the deployment-specific paths for the remaining examples:

```bash
export DAYFINCH_DEPLOYMENT_COMPOSE_FILE=/etc/dayfinch/compose.production.yaml
export DAYFINCH_BACKUP_DESTINATION=/var/backups/dayfinch
set -a
. /etc/dayfinch/production.env
set +a
```

Validate the resolved deployment without rendering it into an incident ticket or
other persistent log, because Compose output can contain secrets:

```bash
docker compose -f compose.yaml -f "$DAYFINCH_DEPLOYMENT_COMPOSE_FILE" config --quiet
```

## Deploy and migrate

Dayfinch applies forward-only PostgreSQL migrations on application startup under a
database advisory lock. There is no separate migration command and no automatic
downgrade. Use this sequence for every release:

1. Record the release tag/commit, operator, UTC start time, expected schema version,
   change window, rollback artifact, and backup destination.
2. Run the production preflight above.
3. Create the pre-deploy backup. The wrapper stops the server, backs up PostgreSQL
   plus referenced object versions, and restores the server through an exit trap:

   ```bash
   scripts/scheduled_backup.sh
   ```

4. Confirm that the new `.dfbackup` exists on the protected destination, is
   non-empty, has an off-site copy, and is associated with the escrowed key version.
5. Fetch the reviewed release and build/pull immutable images. Never deploy a dirty
   checkout:

   ```bash
   git status --short
   git rev-parse HEAD
   docker compose -f compose.yaml -f "$DAYFINCH_DEPLOYMENT_COMPOSE_FILE" build
   ```

6. Start one application replica first. Its startup performs the migration:

   ```bash
   docker compose -f compose.yaml -f "$DAYFINCH_DEPLOYMENT_COMPOSE_FILE" up -d postgres
   docker compose -f compose.yaml -f "$DAYFINCH_DEPLOYMENT_COMPOSE_FILE" up -d --no-deps dayfinch-server
   curl --fail --silent --show-error https://tracker.example.com/readyz
   ```

7. Confirm the expected `schema_migrations` rows, then start remaining replicas and
   background jobs. Check `/readyz`, protected `/metrics`, logs, login, a read-only
   dashboard request, and one disposable enrolled-device heartbeat.
8. Observe error rate, mean latency, readiness, worker failures, and queue depth for
   at least one normal job interval before closing the change.

## Rollback

1. Remove the release from traffic and stop application writers. Preserve logs,
   correlation IDs, failed job labels, and the exact deployed commit.
2. If the previous application version is explicitly compatible with the current
   schema, deploy that immutable image/commit and require `/readyz` plus the smoke
   checks above.
3. If schema compatibility is unknown or the migration changed stored data, do not
   start old code. Restore the complete pre-deploy archive as described below; this
   rolls PostgreSQL and referenced object versions back together.
4. Do not manually delete rows from `schema_migrations` or hand-edit migrated
   columns. Escalate an unexpected forward-migration failure as a database incident.

## Backup and restore

The scheduled setup and accepted local drill are in
`docs/backup-restore-drill.md`. Production must supply off-site retention, a stable
escrow owner, and tested RPO/RTO.

Create an on-demand backup with the safe wrapper:

```bash
scripts/scheduled_backup.sh
```

Restore only during an approved maintenance window:

1. Verify the archive name, checksum/custody record, key version, target database
   name, and target bucket. Stop every application and worker replica.
2. Run the operations image with the backup mount. Replace the archive name and
   database confirmation with the exact reviewed targets:

   ```bash
   docker compose -f compose.yaml -f "$DAYFINCH_DEPLOYMENT_COMPOSE_FILE" -f compose.backup.yaml stop dayfinch-server
   docker compose -f compose.yaml -f "$DAYFINCH_DEPLOYMENT_COMPOSE_FILE" -f compose.backup.yaml run --rm dayfinch-ops restore --maintenance-confirmed --confirm-database dayfinch /backups/dayfinch-YYYYMMDDTHHMMSSZ.dfbackup
   ```

3. Start one server, require `/readyz`, compare every public-table count and object
   inventory, open sampled screenshots and every sealed invoice through authorized
   routes, and verify unauthorized access still fails.
4. Start remaining replicas only after validation. Record actual RPO/RTO and retain
   the restore evidence. Run a disposable restore drill at least quarterly and after
   PostgreSQL, object-storage, or backup-tool changes.

## Secret and credential rotation

Always take a pre-change backup, update the secret manager first, restart only the
affected services, and run the relevant `check_config.py --category ...` command.

- `TRACKER_ADMIN_PASSWORD`: replace it and restart the server. Confirm owner login;
  do not remove another known-good administrator during the change.
- `TRACKER_SESSION_SECRET`: replacing it invalidates every signed web session.
  Announce the logout window, deploy atomically to all replicas, and test login/CSRF.
- `TRACKER_METRICS_BEARER_TOKEN`: add the new scraper credential, update all
  replicas, verify scrapes, then remove the old credential. Never expose `/metrics`
  without a token during the transition.
- `TRACKER_SCIM_BEARER_TOKEN`, OAuth client secrets, provider webhook secrets, and
  SMTP/S3 credentials: create a new provider credential, deploy it, pass a real
  provider operation, then revoke the old credential. For S3, confirm read/write/
  exact-version delete and bucket versioning before revocation.
- `TRACKER_INTEGRATION_ENCRYPTION_KEYS`: prepend a new `key-id:key` entry and keep
  old keys after it. The scheduled `integration-credential-rewrap` job re-encrypts
  stored credentials. Confirm the old key ID is no longer referenced and provider
  refresh still works before removing it.
- OIDC/SAML keys and certificates: overlap the provider's old/new verification
  material, update pinned metadata/client secret, restart all replicas, and test
  login, logout, replay denial, disabled users, and certificate expiry handling
  before removing the old provider material.
- `DAYFINCH_UPDATE_SIGNING_KEY` and `TRACKER_AGENT_UPDATE_PUBLIC_KEY`: these are a
  cryptographic pair, and existing agents trust the old public key. Do not rotate
  them until an overlap/migration release has been designed and tested.
- `TRACKER_BACKUP_ENCRYPTION_KEY`: a new key protects only new archives. Retain each
  previous key in escrow for every archive still inside retention and prove a
  disposable restore before retiring it.
- `TRACKER_DOCUMENT_ENCRYPTION_KEY`: keep it stable. Rotation is blocked until a
  transactional re-encryption utility and rollback drill exist.

## Device revocation

1. In **Projects & tasks**, open the project, select the tracker in **Trackers**, and
   choose **Revoke token**. Admins, project managers, and permitted Manage-IT roles
   can revoke according to their scope.
2. Confirm a `device.revoked` audit event. A later heartbeat, usage event, location,
   or screenshot upload with that bearer token must return `401`.
3. Revocation does not erase prior time or captures. Delete activity only through
   the authorized UI and applicable retention/privacy process.
4. To return the employee to service, create a new enrollment and transfer its
   one-time configuration privately. Do not re-enable or redistribute a suspected
   token.

## Alert first response

For every alert, record start time, affected instance/route/job/queue labels, recent
deployment or secret change, correlation IDs, and customer impact. Silence an alert
only with an incident owner and expiry; never delete queued work to clear a metric.

| Alert | First response |
| --- | --- |
| `DayfinchTargetDown` | Check the scraper path and protected token, then `/livez`, container/process state, load balancer health, and host/network health. Restart only after preserving crash logs; roll back a correlated deployment. |
| `DayfinchNotReady` | Remove the replica from traffic, call `/readyz`, and inspect PostgreSQL reachability, credentials, pool exhaustion, locks, disk, and failover status. Do not bypass readiness or run competing manual migrations. |
| `DayfinchMetricsCollectionFailed` | Confirm normal requests and `/readyz` separately, then inspect the metrics-collection log and PostgreSQL aggregate queries. Restore collection before trusting queue dashboards; HTTP counters remain usable. |
| `DayfinchHighErrorRate` | Break down 5xx counters by template route and instance, correlate structured logs, and reproduce one safe request. Check the latest deploy, database/S3/provider health, and resource pressure; roll back if release-correlated. |
| `DayfinchHighMeanLatency` | Compare instances/routes, CPU/RSS, database pool and locks, S3/provider latency, and queue depth. Shed nonessential jobs or scale only within the tested capacity envelope; do not hide the alert by raising its threshold during an incident. |
| `DayfinchScreenshotIngestFailures` | Check `/api/v1/activity` logs, PostgreSQL, S3 authentication/versioning/KMS, object latency, and upload limits. Agents retain encrypted captures for retry; restore dependencies and verify queue drain/idempotency before declaring recovery. |
| `DayfinchBackgroundJobFailures` | Use the `job` label to inspect the matching structured log, lease owner, provider response class, credential expiry, and next retry. Fix the dependency/configuration; preserve idempotency and never manually replay without checking provider commit state. |
| `DayfinchQueueBacklogHigh` | Use the `queue` label to identify Jira worklogs, Asana comments, or Slack notifications. Check the matching job alert, provider status/rate limits, credentials, and oldest item age; restore the worker and watch an orderly idempotent drain. |

After recovery, require the alert to resolve for its full evaluation window, verify
one user-visible workflow, document root cause/corrective action, and schedule a
failure drill if the incident exposed an untested path.

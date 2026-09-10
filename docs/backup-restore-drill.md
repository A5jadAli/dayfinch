# Local backup and restore drill

> **Local isolated restore evidence, not a production backup claim.** This drill
> used a test-only encryption key, local PostgreSQL, and a versioned MinIO stand-in.
> It does not prove off-site retention, production RPO/RTO, escrow recovery, or a
> managed database/object-store restore.

## Scheduled-backup example

`compose.backup.yaml` adds a host backup destination to the operations container.
`scripts/scheduled_backup.sh` stops application writes, creates a uniquely named
encrypted archive, and restarts the server through an exit trap. The destination
is configured outside the repository:

```bash
export DAYFINCH_BACKUP_DESTINATION=/var/backups/dayfinch
docker compose -f compose.yaml -f compose.backup.yaml config --quiet
scripts/scheduled_backup.sh
```

If a deployment needs an additional Compose override, set
`DAYFINCH_DEPLOYMENT_COMPOSE_FILE` to that file. The local stand-in stack uses
`compose.local.yaml`; production must use its own reviewed deployment override.

The host must provide a stable, separately escrowed
`TRACKER_BACKUP_ENCRYPTION_KEY`; the script intentionally fails if the destination
or key is absent. Keep the destination on encrypted storage and replicate it to an
access-controlled off-site location using the deployment platform.

Example systemd units are in `deploy/systemd/dayfinch-backup.service` and
`deploy/systemd/dayfinch-backup.timer`. Before enabling them:

1. Install the checkout at `/opt/dayfinch` or adjust `WorkingDirectory` and
   `ExecStart`.
2. Create a `dayfinch` service account with read access to the checkout, membership
   in the local Docker group, and write access only to the backup destination.
3. Put `DAYFINCH_BACKUP_DESTINATION`, `TRACKER_BACKUP_ENCRYPTION_KEY`, and the
   Compose interpolation secrets in mode-`0600` `/etc/dayfinch/backup.env`.
4. Adjust `ReadWritePaths` if the destination differs from
   `/var/backups/dayfinch`, then run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dayfinch-backup.timer
systemctl list-timers dayfinch-backup.timer
```

The example runs daily at 02:15 UTC with up to 15 minutes of jitter. A deployment
must choose its schedule, retention, off-site copy, and alerting from its own RPO,
RTO, and compliance requirements.

The wrapper was also executed once against the local MinIO override with a fresh
test-only key. It created an 802-object archive, restored the server through its
exit trap, waited for the Compose health check, and left `/readyz` returning 200.

## Accepted isolated drill: 2026-09-10

The first attempted archive destination was under `/tmp`; Docker Desktop rejected
that host mount before creating any drill database or bucket, and the exit trap
restored the server. The accepted run used the ignored, Docker-shared
`runtime/backups/` directory.

To avoid risking existing local data, the accepted procedure was:

1. Stop the source server briefly and create an encrypted seed archive from the
   current local database and exact MinIO object versions; restart the server.
2. Create the explicitly disposable database
   `dayfinch_wi7_drill_20260910` and versioned bucket
   `dayfinch-wi7-drill-20260910`.
3. Restore the seed archive into those isolated targets, producing realistic local
   data, and write a secret-free inventory of every public table and referenced
   object's key, size, and SHA-256 digest.
4. Back up the isolated targets, drop only that disposable database, delete every
   version from only that disposable bucket, and recreate both empty targets.
5. Restore the new archive and generate the same inventory again. A byte-for-byte
   comparison of the canonical JSON inventories succeeded; `/readyz` on the
   original local stack returned 200 afterward.

### Measurements

| Measurement | Actual |
| --- | ---: |
| Encrypted drill archive | 2,498,597 bytes |
| Seed archive duration | 10.578 s |
| Isolated backup duration | 10.331 s |
| Destroy and recreate duration | 5.580 s |
| Restore duration | 19.675 s |
| Public tables compared | 69 |
| Total rows compared | 1,860 |
| Referenced objects compared | 802 |

All 69 per-table counts matched. The non-zero counts were:

| Table | Before | After |
| --- | ---: | ---: |
| `activity_records` | 802 | 802 |
| `agent_state_events` | 823 | 823 |
| `audit_events` | 38 | 38 |
| `devices` | 14 | 14 |
| `invitations` | 13 | 13 |
| `organization_settings` | 1 | 1 |
| `project_members` | 16 | 16 |
| `projects` | 5 | 5 |
| `request_rate_limits` | 16 | 16 |
| `schema_migrations` | 61 | 61 |
| `timesheets` | 13 | 13 |
| `users` | 14 | 14 |
| `work_session_segments` | 22 | 22 |
| `work_sessions` | 22 | 22 |

The other 55 public tables were present with zero rows before and after. For all
802 activity references, object key, byte size, and SHA-256 digest matched. MinIO
assigned new VersionIds on restore, and the restore updated each database reference
to its new version. Old version history and unreferenced objects are intentionally
not reproduced by the backup format.

`scripts/backup_inventory.py` generated the comparison without emitting database
credentials, backup keys, device tokens, or object bytes. The encrypted archives
and inventory files remain under the ignored `runtime/backups/wi7.UZMp3P/` local
test directory; they are not production backups because the ephemeral drill key
was not escrowed.

## Remaining production work

The mechanics passed locally, but production remains blocked on a real destination,
retention policy, off-site replication, monitoring, a stable escrowed encryption
key, representative production-scale timing, and an operator-approved RPO/RTO.

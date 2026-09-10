from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from psycopg.errors import UniqueViolation

from .base import RepositoryMixin, utc_now

_REPOSITORY_NAME = re.compile(r"^[^/\s]{1,100}/[^/\s]{1,100}$")
_JIRA_PROJECT_ID = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
_JIRA_PROJECT_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,49}$")
_ATLASSIAN_ACCOUNT_ID = re.compile(r"^[^\s\x00-\x1f\x7f]{1,255}$")
_ASANA_GID = re.compile(r"^[^\s/\x00-\x1f\x7f]{1,200}$")
_SLACK_ID = re.compile(r"^[A-Z][A-Z0-9]{1,30}$")


def _github_task_name(repository: str, number: int, title: str) -> str:
    prefix = f"{repository} #{number}: "
    cleaned = " ".join(title.replace("\x00", "").split()) or "Untitled issue"
    return (prefix + cleaned)[:500]


def _jira_task_name(issue_key: str, summary: str) -> str:
    cleaned = " ".join(summary.replace("\x00", "").split()) or "Untitled issue"
    return f"{issue_key}: {cleaned}"[:500]


def _asana_task_name(task_gid: str, title: str) -> str:
    cleaned = " ".join(title.replace("\x00", "").split()) or "Untitled task"
    return f"Asana {task_gid}: {cleaned}"[:500]


def _close_task_sessions(
    connection, task_ids: list[str], observed_at: datetime
) -> None:
    if not task_ids:
        return
    connection.execute(
        """UPDATE work_session_segments SET ended_at=%s
           WHERE session_id IN (
               SELECT id FROM work_sessions
               WHERE task_id=ANY(%s::uuid[]) AND ended_at IS NULL
           ) AND ended_at IS NULL""",
        (observed_at, task_ids),
    )
    connection.execute(
        """UPDATE work_breaks SET ended_at=%s
           WHERE session_id IN (
               SELECT id FROM work_sessions
               WHERE task_id=ANY(%s::uuid[]) AND ended_at IS NULL
           ) AND ended_at IS NULL""",
        (observed_at, task_ids),
    )
    connection.execute(
        """UPDATE work_sessions SET status='stopped',ended_at=%s,updated_at=%s
           WHERE task_id=ANY(%s::uuid[]) AND ended_at IS NULL""",
        (observed_at, observed_at, task_ids),
    )


class IntegrationsRepository(RepositoryMixin):
    def store_pending_oauth_credentials(
        self,
        pending_id: str,
        user_id: str,
        provider: str,
        key_id: str,
        ciphertext: bytes,
        expires_at: datetime,
    ) -> None:
        with self.connect() as connection:
            row = connection.execute(
                """INSERT INTO integration_oauth_pending(
                       id,user_id,provider,key_id,ciphertext,expires_at,created_at
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(id) DO NOTHING
                   RETURNING id""",
                (
                    pending_id,
                    user_id,
                    provider,
                    key_id,
                    ciphertext,
                    expires_at,
                    utc_now(),
                ),
            ).fetchone()
            if not row:
                raise ValueError("Pending OAuth authorization identity already exists")

    def get_pending_oauth_credentials(
        self, pending_id: str, user_id: str, provider: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT id integration_id,user_id,provider,key_id,ciphertext,
                          expires_at access_expires_at,1 revision
                   FROM integration_oauth_pending
                   WHERE id=%s AND user_id=%s AND provider=%s AND expires_at>%s""",
                (pending_id, user_id, provider, utc_now()),
            ).fetchone()
        return dict(row) if row else None

    def delete_pending_oauth_credentials(
        self, pending_id: str, user_id: str, provider: str
    ) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """DELETE FROM integration_oauth_pending
                   WHERE id=%s AND user_id=%s AND provider=%s""",
                (pending_id, user_id, provider),
            )
        return result.rowcount == 1

    def purge_pending_oauth_credentials(
        self, observed_at: datetime | None = None
    ) -> int:
        observed_at = observed_at or utc_now()
        with self.connect() as connection:
            result = connection.execute(
                "DELETE FROM integration_oauth_pending WHERE expires_at<=%s",
                (observed_at,),
            )
        return result.rowcount

    def integration_credentials_requiring_rewrap(
        self, primary_key_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("Invalid credential rewrap limit")
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT credentials.integration_id,credentials.provider,
                          credentials.user_id
                   FROM (
                       SELECT c.integration_id,i.provider,NULL::uuid user_id,
                              c.updated_at
                       FROM integration_credentials c
                       JOIN integrations i ON i.id=c.integration_id
                       WHERE c.key_id<>%s
                       UNION ALL
                       SELECT c.integration_id,i.provider,c.user_id,c.updated_at
                       FROM integration_user_credentials c
                       JOIN integrations i ON i.id=c.integration_id
                       WHERE c.key_id<>%s
                   ) credentials
                   ORDER BY credentials.updated_at,credentials.integration_id,
                            credentials.user_id NULLS FIRST
                   LIMIT %s""",
                (primary_key_id, primary_key_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def store_integration_credentials(
        self,
        integration_id: str,
        provider: str,
        key_id: str,
        ciphertext: bytes,
        access_expires_at: datetime | None,
    ) -> int:
        now = utc_now()
        with self.connect() as connection:
            integration = connection.execute(
                "SELECT id FROM integrations WHERE id=%s AND provider=%s FOR SHARE",
                (integration_id, provider),
            ).fetchone()
            if not integration:
                raise ValueError("Integration not found")
            row = connection.execute(
                """INSERT INTO integration_credentials(
                       integration_id,key_id,ciphertext,revision,access_expires_at,
                       created_at,updated_at
                   ) VALUES (%s,%s,%s,1,%s,%s,%s)
                   ON CONFLICT(integration_id) DO UPDATE SET
                       key_id=EXCLUDED.key_id,ciphertext=EXCLUDED.ciphertext,
                       revision=integration_credentials.revision+1,
                       access_expires_at=EXCLUDED.access_expires_at,
                       refresh_claim_token=NULL,refresh_claim_until=NULL,
                       updated_at=EXCLUDED.updated_at
                   WHERE integration_credentials.refresh_claim_until IS NULL
                      OR integration_credentials.refresh_claim_until < %s
                   RETURNING revision""",
                (
                    integration_id,
                    key_id,
                    ciphertext,
                    access_expires_at,
                    now,
                    now,
                    now,
                ),
            ).fetchone()
            if not row:
                raise ValueError("Integration credential refresh is active")
        return int(row["revision"])

    def get_integration_credentials(
        self, integration_id: str, provider: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT c.*,i.provider FROM integration_credentials c
                   JOIN integrations i ON i.id=c.integration_id
                   WHERE c.integration_id=%s AND i.provider=%s""",
                (integration_id, provider),
            ).fetchone()
        return dict(row) if row else None

    def rotate_integration_credentials(
        self,
        integration_id: str,
        provider: str,
        expected_revision: int,
        key_id: str,
        ciphertext: bytes,
    ) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE integration_credentials c
                   SET key_id=%s,ciphertext=%s,revision=c.revision+1,updated_at=%s
                   FROM integrations i
                   WHERE c.integration_id=%s AND c.revision=%s
                     AND c.refresh_claim_token IS NULL
                     AND i.id=c.integration_id AND i.provider=%s""",
                (
                    key_id,
                    ciphertext,
                    utc_now(),
                    integration_id,
                    expected_revision,
                    provider,
                ),
            )
        return result.rowcount == 1

    def claim_integration_credentials(
        self,
        integration_id: str,
        provider: str,
        claim_token: str,
        observed_at: datetime,
        *,
        lease_seconds: int = 90,
    ) -> dict[str, Any] | None:
        if not 15 <= lease_seconds <= 600:
            raise ValueError("Invalid credential refresh lease")
        try:
            claim_token = str(uuid.UUID(claim_token))
        except (ValueError, AttributeError) as exc:
            raise ValueError("Invalid credential refresh claim") from exc
        with self.connect() as connection:
            row = connection.execute(
                """UPDATE integration_credentials c
                   SET refresh_claim_token=%s,refresh_claim_until=%s,updated_at=%s
                   FROM integrations i
                   WHERE c.integration_id=%s AND i.id=c.integration_id
                     AND i.provider=%s
                     AND (c.refresh_claim_until IS NULL OR c.refresh_claim_until <= %s)
                   RETURNING c.*,i.provider""",
                (
                    claim_token,
                    observed_at + timedelta(seconds=lease_seconds),
                    observed_at,
                    integration_id,
                    provider,
                    observed_at,
                ),
            ).fetchone()
        return dict(row) if row else None

    def release_integration_credential_claim(
        self, integration_id: str, provider: str, claim_token: str
    ) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE integration_credentials c
                   SET refresh_claim_token=NULL,refresh_claim_until=NULL,updated_at=%s
                   FROM integrations i
                   WHERE c.integration_id=%s AND c.refresh_claim_token=%s
                     AND i.id=c.integration_id AND i.provider=%s""",
                (utc_now(), integration_id, claim_token, provider),
            )
        return result.rowcount == 1

    def replace_claimed_integration_credentials(
        self,
        integration_id: str,
        provider: str,
        claim_token: str,
        expected_revision: int,
        key_id: str,
        ciphertext: bytes,
        access_expires_at: datetime | None,
    ) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """UPDATE integration_credentials c
                   SET key_id=%s,ciphertext=%s,revision=c.revision+1,
                       access_expires_at=%s,refresh_claim_token=NULL,
                       refresh_claim_until=NULL,updated_at=%s
                   FROM integrations i
                   WHERE c.integration_id=%s AND c.revision=%s
                     AND c.refresh_claim_token=%s
                     AND i.id=c.integration_id AND i.provider=%s
                   RETURNING c.revision""",
                (
                    key_id,
                    ciphertext,
                    access_expires_at,
                    utc_now(),
                    integration_id,
                    expected_revision,
                    claim_token,
                    provider,
                ),
            ).fetchone()
            if not row:
                raise ValueError("Integration credential refresh claim was lost")
        return int(row["revision"])

    def store_user_integration_credentials(
        self,
        integration_id: str,
        user_id: str,
        provider: str,
        key_id: str,
        ciphertext: bytes,
        access_expires_at: datetime | None,
    ) -> int:
        now = utc_now()
        with self.connect() as connection:
            authorized = connection.execute(
                """SELECT i.id FROM integrations i JOIN users u ON u.id=%s
                   WHERE i.id=%s AND i.provider=%s AND u.enabled=TRUE
                   FOR SHARE OF i,u""",
                (user_id, integration_id, provider),
            ).fetchone()
            if not authorized:
                raise ValueError("Integration user not found")
            row = connection.execute(
                """INSERT INTO integration_user_credentials(
                       integration_id,user_id,key_id,ciphertext,revision,
                       access_expires_at,created_at,updated_at
                   ) VALUES (%s,%s,%s,%s,1,%s,%s,%s)
                   ON CONFLICT(integration_id,user_id) DO UPDATE SET
                       key_id=EXCLUDED.key_id,ciphertext=EXCLUDED.ciphertext,
                       revision=integration_user_credentials.revision+1,
                       access_expires_at=EXCLUDED.access_expires_at,
                       refresh_claim_token=NULL,refresh_claim_until=NULL,
                       updated_at=EXCLUDED.updated_at
                   WHERE integration_user_credentials.refresh_claim_until IS NULL
                      OR integration_user_credentials.refresh_claim_until < %s
                   RETURNING revision""",
                (
                    integration_id,
                    user_id,
                    key_id,
                    ciphertext,
                    access_expires_at,
                    now,
                    now,
                    now,
                ),
            ).fetchone()
            if not row:
                raise ValueError("Integration user credential refresh is active")
        return int(row["revision"])

    def get_user_integration_credentials(
        self, integration_id: str, user_id: str, provider: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT c.*,i.provider FROM integration_user_credentials c
                   JOIN integrations i ON i.id=c.integration_id
                   WHERE c.integration_id=%s AND c.user_id=%s
                     AND i.provider=%s""",
                (integration_id, user_id, provider),
            ).fetchone()
        return dict(row) if row else None

    def delete_user_integration_credentials(
        self, integration_id: str, user_id: str, provider: str
    ) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """DELETE FROM integration_user_credentials c USING integrations i
                   WHERE c.integration_id=%s AND c.user_id=%s
                     AND i.id=c.integration_id AND i.provider=%s""",
                (integration_id, user_id, provider),
            )
        return result.rowcount == 1

    def rotate_user_integration_credentials(
        self,
        integration_id: str,
        user_id: str,
        provider: str,
        expected_revision: int,
        key_id: str,
        ciphertext: bytes,
    ) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE integration_user_credentials c
                   SET key_id=%s,ciphertext=%s,revision=c.revision+1,updated_at=%s
                   FROM integrations i
                   WHERE c.integration_id=%s AND c.user_id=%s AND c.revision=%s
                     AND c.refresh_claim_token IS NULL
                     AND i.id=c.integration_id AND i.provider=%s""",
                (
                    key_id,
                    ciphertext,
                    utc_now(),
                    integration_id,
                    user_id,
                    expected_revision,
                    provider,
                ),
            )
        return result.rowcount == 1

    def claim_user_integration_credentials(
        self,
        integration_id: str,
        user_id: str,
        provider: str,
        claim_token: str,
        observed_at: datetime,
        *,
        lease_seconds: int = 90,
    ) -> dict[str, Any] | None:
        if not 15 <= lease_seconds <= 600:
            raise ValueError("Invalid credential refresh lease")
        try:
            claim_token = str(uuid.UUID(claim_token))
        except (ValueError, AttributeError) as exc:
            raise ValueError("Invalid credential refresh claim") from exc
        with self.connect() as connection:
            row = connection.execute(
                """UPDATE integration_user_credentials c
                   SET refresh_claim_token=%s,refresh_claim_until=%s,updated_at=%s
                   FROM integrations i,users u
                   WHERE c.integration_id=%s AND c.user_id=%s
                     AND i.id=c.integration_id AND i.provider=%s
                     AND u.id=c.user_id AND u.enabled=TRUE
                     AND (c.refresh_claim_until IS NULL OR c.refresh_claim_until <= %s)
                   RETURNING c.*,i.provider""",
                (
                    claim_token,
                    observed_at + timedelta(seconds=lease_seconds),
                    observed_at,
                    integration_id,
                    user_id,
                    provider,
                    observed_at,
                ),
            ).fetchone()
        return dict(row) if row else None

    def release_user_integration_credential_claim(
        self,
        integration_id: str,
        user_id: str,
        provider: str,
        claim_token: str,
    ) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE integration_user_credentials c
                   SET refresh_claim_token=NULL,refresh_claim_until=NULL,updated_at=%s
                   FROM integrations i
                   WHERE c.integration_id=%s AND c.user_id=%s
                     AND c.refresh_claim_token=%s
                     AND i.id=c.integration_id AND i.provider=%s""",
                (utc_now(), integration_id, user_id, claim_token, provider),
            )
        return result.rowcount == 1

    def replace_claimed_user_integration_credentials(
        self,
        integration_id: str,
        user_id: str,
        provider: str,
        claim_token: str,
        expected_revision: int,
        key_id: str,
        ciphertext: bytes,
        access_expires_at: datetime | None,
    ) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """UPDATE integration_user_credentials c
                   SET key_id=%s,ciphertext=%s,revision=c.revision+1,
                       access_expires_at=%s,refresh_claim_token=NULL,
                       refresh_claim_until=NULL,updated_at=%s
                   FROM integrations i
                   WHERE c.integration_id=%s AND c.user_id=%s AND c.revision=%s
                     AND c.refresh_claim_token=%s
                     AND i.id=c.integration_id AND i.provider=%s
                   RETURNING c.revision""",
                (
                    key_id,
                    ciphertext,
                    access_expires_at,
                    utc_now(),
                    integration_id,
                    user_id,
                    expected_revision,
                    claim_token,
                    provider,
                ),
            ).fetchone()
            if not row:
                raise ValueError("Integration user credential refresh claim was lost")
        return int(row["revision"])

    def expire_user_integration_access(
        self,
        integration_id: str,
        user_id: str,
        provider: str,
        observed_at: datetime,
    ) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE integration_user_credentials c
                   SET access_expires_at=%s,updated_at=%s
                   FROM integrations i
                   WHERE c.integration_id=%s AND c.user_id=%s
                     AND i.id=c.integration_id AND i.provider=%s""",
                (observed_at, observed_at, integration_id, user_id, provider),
            )
        return result.rowcount == 1

    def expire_integration_access(
        self, integration_id: str, provider: str, observed_at: datetime
    ) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE integration_credentials c SET access_expires_at=%s,
                          updated_at=%s
                   FROM integrations i
                   WHERE c.integration_id=%s AND i.id=c.integration_id
                     AND i.provider=%s""",
                (observed_at, observed_at, integration_id, provider),
            )
        return result.rowcount == 1

    def integration_sync_snapshot_at(self) -> datetime:
        with self.connect() as connection:
            row = connection.execute("SELECT clock_timestamp() observed_at").fetchone()
        value = row["observed_at"]
        return datetime.fromisoformat(value) if isinstance(value, str) else value

    def upsert_jira_site(
        self,
        cloud_id: str,
        site_name: str,
        site_url: str,
        created_by_user_id: str,
    ) -> str:
        resource_key = cloud_id.strip()
        name = " ".join(site_name.replace("\x00", "").split())
        resource_url = site_url.strip().rstrip("/")
        if (
            not _JIRA_PROJECT_ID.fullmatch(resource_key)
            or not name
            or len(name) > 255
            or not resource_url
            or len(resource_url) > 1000
        ):
            raise ValueError("Jira returned an invalid site")
        integration_id = str(uuid.uuid4())
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """INSERT INTO integrations(
                       id,provider,display_name,enabled,provider_resource_key,
                       provider_resource_url,account_login,account_type,
                       created_by_user_id,created_at,updated_at,next_sync_at
                   ) VALUES (%s,'jira',%s,TRUE,%s,%s,%s,'Cloud',%s,%s,%s,%s)
                   ON CONFLICT(provider,provider_resource_key)
                     WHERE provider='jira' AND provider_resource_key<>''
                   DO UPDATE SET display_name=EXCLUDED.display_name,
                     provider_resource_url=EXCLUDED.provider_resource_url,
                     account_login=EXCLUDED.account_login,enabled=TRUE,
                     created_by_user_id=EXCLUDED.created_by_user_id,
                     updated_at=EXCLUDED.updated_at,next_sync_at=EXCLUDED.next_sync_at,
                     last_error_code='',consecutive_failures=0,
                     sync_claim_token=NULL,sync_claim_until=NULL
                   RETURNING id""",
                (
                    integration_id,
                    f"Jira · {name}"[:120],
                    resource_key,
                    resource_url,
                    name,
                    created_by_user_id,
                    now,
                    now,
                    now,
                ),
            ).fetchone()
            connection.execute(
                """UPDATE jira_project_mappings
                   SET last_full_sync_at=NULL,last_incremental_sync_at=NULL,
                       updated_at=%s
                   WHERE integration_id=%s""",
                (now, row["id"]),
            )
        return row["id"]

    def list_jira_integrations(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT i.*,
                          (SELECT COUNT(*) FROM jira_project_mappings m
                           WHERE m.integration_id=i.id) mapping_count,
                          (SELECT COUNT(*) FROM tasks t
                           WHERE t.integration_id=i.id) synced_task_count
                   FROM integrations i WHERE i.provider='jira'
                   ORDER BY i.enabled DESC,lower(i.account_login)"""
            ).fetchall()
        return [dict(row) for row in rows]

    def get_jira_integration(self, integration_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT i.*,
                          (SELECT COUNT(*) FROM jira_project_mappings m
                           WHERE m.integration_id=i.id) mapping_count,
                          (SELECT COUNT(*) FROM tasks t
                           WHERE t.integration_id=i.id) synced_task_count
                   FROM integrations i
                   WHERE i.id=%s AND i.provider='jira'""",
                (integration_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_jira_integration_by_resource(
        self, resource_key: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM integrations
                   WHERE provider='jira' AND provider_resource_key=%s""",
                (resource_key,),
            ).fetchone()
        return dict(row) if row else None

    def upsert_jira_user_connection(
        self,
        integration_id: str,
        user_id: str,
        atlassian_account_id: str,
        display_name: str,
        credential_kind: str,
    ) -> dict[str, Any]:
        account_id = atlassian_account_id.strip()
        name = " ".join(display_name.replace("\x00", "").split())
        if (
            not _ATLASSIAN_ACCOUNT_ID.fullmatch(account_id)
            or not name
            or len(name) > 255
            or credential_kind not in {"site", "member"}
        ):
            raise ValueError("Jira returned an invalid user identity")
        now = utc_now()
        try:
            with self.connect() as connection:
                authorization = connection.execute(
                    """SELECT i.created_by_user_id,u.enabled,
                              EXISTS(
                                  SELECT 1 FROM integration_credentials c
                                  WHERE c.integration_id=i.id
                              ) site_credential,
                              EXISTS(
                                  SELECT 1 FROM integration_user_credentials c
                                  WHERE c.integration_id=i.id AND c.user_id=u.id
                              ) member_credential
                       FROM integrations i JOIN users u ON u.id=%s
                       WHERE i.id=%s AND i.provider='jira' AND i.enabled=TRUE
                       FOR SHARE OF i,u""",
                    (user_id, integration_id),
                ).fetchone()
                if not authorization or not authorization["enabled"]:
                    raise ValueError("Jira integration user is unavailable")
                prior_export_identity = connection.execute(
                    """SELECT atlassian_account_id FROM jira_worklog_exports
                       WHERE integration_id=%s AND user_id=%s
                         AND atlassian_account_id<>%s
                       LIMIT 1""",
                    (integration_id, user_id, account_id),
                ).fetchone()
                if prior_export_identity:
                    raise ValueError(
                        "Reconnect the same Jira account that owns this user's "
                        "existing worklogs"
                    )
                if credential_kind == "site":
                    if authorization["created_by_user_id"] != user_id:
                        raise ValueError(
                            "Only the Jira site connector can use this grant"
                        )
                    if not authorization["site_credential"]:
                        raise ValueError("Jira site authorization is unavailable")
                elif not authorization["member_credential"]:
                    raise ValueError("Jira member authorization is unavailable")
                if credential_kind == "site":
                    connection.execute(
                        """UPDATE jira_user_connections SET enabled=FALSE,updated_at=%s
                           WHERE integration_id=%s AND credential_kind='site'
                             AND user_id<>%s""",
                        (now, integration_id, user_id),
                    )
                    connection.execute(
                        """DELETE FROM integration_user_credentials
                           WHERE integration_id=%s AND user_id=%s""",
                        (integration_id, user_id),
                    )
                row = connection.execute(
                    """INSERT INTO jira_user_connections(
                           integration_id,user_id,atlassian_account_id,display_name,
                           credential_kind,time_sync_mode,enabled,connected_at,updated_at,
                           next_worklog_sync_at,last_worklog_error_code
                       ) VALUES (%s,%s,%s,%s,%s,'daily',TRUE,%s,%s,%s,'')
                       ON CONFLICT(integration_id,user_id) DO UPDATE SET
                           atlassian_account_id=EXCLUDED.atlassian_account_id,
                           display_name=EXCLUDED.display_name,
                           credential_kind=EXCLUDED.credential_kind,enabled=TRUE,
                           connected_at=EXCLUDED.connected_at,
                           updated_at=EXCLUDED.updated_at,
                           next_worklog_sync_at=EXCLUDED.next_worklog_sync_at,
                           last_worklog_error_code=''
                       RETURNING *""",
                    (
                        integration_id,
                        user_id,
                        account_id,
                        name,
                        credential_kind,
                        now,
                        now,
                        now,
                    ),
                ).fetchone()
                open_period = connection.execute(
                    """SELECT id,atlassian_account_id
                       FROM jira_user_authorization_periods
                       WHERE integration_id=%s AND user_id=%s AND ended_at IS NULL
                       FOR UPDATE""",
                    (integration_id, user_id),
                ).fetchone()
                if open_period and open_period["atlassian_account_id"] != account_id:
                    connection.execute(
                        """UPDATE jira_user_authorization_periods
                           SET ended_at=%s WHERE id=%s""",
                        (now, open_period["id"]),
                    )
                    open_period = None
                if not open_period:
                    connection.execute(
                        """INSERT INTO jira_user_authorization_periods(
                               id,integration_id,user_id,atlassian_account_id,
                               started_at,created_at
                           ) VALUES (%s,%s,%s,%s,%s,%s)""",
                        (
                            str(uuid.uuid4()),
                            integration_id,
                            user_id,
                            account_id,
                            now,
                            now,
                        ),
                    )
        except UniqueViolation as exc:
            raise ValueError(
                "That Jira account is already connected to another Dayfinch user"
            ) from exc
        return dict(row)

    def jira_worklog_identity_conflicts(
        self, integration_id: str, user_id: str, atlassian_account_id: str
    ) -> bool:
        account_id = atlassian_account_id.strip()
        if not _ATLASSIAN_ACCOUNT_ID.fullmatch(account_id):
            return True
        with self.connect() as connection:
            row = connection.execute(
                """SELECT 1 FROM jira_worklog_exports
                   WHERE integration_id=%s AND user_id=%s
                     AND atlassian_account_id<>%s LIMIT 1""",
                (integration_id, user_id, account_id),
            ).fetchone()
        return row is not None

    def jira_user_connection(
        self, integration_id: str, user_id: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT c.*,u.email,u.full_name,u.enabled user_enabled,
                          i.enabled integration_enabled
                   FROM jira_user_connections c
                   JOIN users u ON u.id=c.user_id
                   JOIN integrations i ON i.id=c.integration_id
                   WHERE c.integration_id=%s AND c.user_id=%s
                     AND i.provider='jira'""",
                (integration_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def list_jira_user_connections(self, integration_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT c.*,u.email,u.full_name,u.enabled user_enabled
                   FROM jira_user_connections c JOIN users u ON u.id=c.user_id
                   WHERE c.integration_id=%s
                   ORDER BY c.enabled DESC,lower(COALESCE(u.full_name,u.email))""",
                (integration_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_jira_user_sync_mode(
        self, integration_id: str, user_id: str, sync_mode: str
    ) -> None:
        if sync_mode not in {"off", "hourly", "daily", "delayed"}:
            raise ValueError("Choose a valid Jira time synchronization mode")
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE jira_user_connections SET time_sync_mode=%s,updated_at=%s
                   ,next_worklog_sync_at=%s,last_worklog_error_code=''
                   WHERE integration_id=%s AND user_id=%s AND enabled=TRUE""",
                (sync_mode, utc_now(), utc_now(), integration_id, user_id),
            )
            if result.rowcount != 1:
                raise ValueError("Jira user connection not found")

    def stage_due_jira_worklogs(
        self,
        observed_at: datetime,
        *,
        connection_limit: int = 10,
        day_limit: int = 250,
    ) -> int:
        """Turn dirty source days into a compact, idempotent delivery outbox."""
        if observed_at.tzinfo is None:
            raise ValueError("Jira worklog observation time must include a timezone")
        if not 1 <= connection_limit <= 100 or not 1 <= day_limit <= 1000:
            raise ValueError("Invalid Jira worklog staging limit")
        staged_count = 0
        with self.connect() as connection:
            connections = connection.execute(
                """SELECT c.* FROM jira_user_connections c
                   JOIN integrations i ON i.id=c.integration_id
                   JOIN users u ON u.id=c.user_id
                   WHERE c.enabled=TRUE AND c.time_sync_mode<>'off'
                     AND c.next_worklog_sync_at<=%s
                     AND i.provider='jira' AND i.enabled=TRUE AND u.enabled=TRUE
                   ORDER BY c.next_worklog_sync_at,c.integration_id,c.user_id
                   FOR UPDATE OF c SKIP LOCKED LIMIT %s""",
                (observed_at, connection_limit),
            ).fetchall()
            for jira_connection in connections:
                result = connection.execute(
                    """WITH selected AS (
                           SELECT d.*,c.atlassian_account_id
                           FROM jira_worklog_dirty_days d
                           JOIN jira_user_connections c
                             ON c.integration_id=d.integration_id
                            AND c.user_id=d.user_id
                           WHERE d.integration_id=%s AND d.user_id=%s
                             AND (
                               (c.time_sync_mode='hourly'
                                AND d.work_date<=%s::timestamptz::date)
                               OR (c.time_sync_mode='daily'
                                   AND d.work_date<%s::timestamptz::date)
                               OR (c.time_sync_mode='delayed'
                                   AND d.work_date<%s::timestamptz::date-1)
                             )
                           ORDER BY d.work_date,d.changed_at,d.task_id
                           FOR UPDATE OF d SKIP LOCKED LIMIT %s
                       ), totals AS (
                           SELECT s.*,
                                  LEAST(COALESCE(source.seconds,0),2147483647)::BIGINT
                                      desired_seconds,
                                  source.started_at desired_started_at
                           FROM selected s
                           LEFT JOIN LATERAL (
                               SELECT SUM(entry.seconds)::BIGINT seconds,
                                      MIN(entry.started_at) started_at
                               FROM (
                                   SELECT EXTRACT(EPOCH FROM (
                                              LEAST(seg.ended_at,
                                                COALESCE(period.ended_at,'infinity'),
                                                (s.work_date::timestamp
                                                 AT TIME ZONE 'UTC')+INTERVAL '1 day')
                                              - GREATEST(seg.started_at,period.started_at,
                                                s.work_date::timestamp
                                                AT TIME ZONE 'UTC')
                                          ))::BIGINT seconds,
                                          GREATEST(seg.started_at,period.started_at,
                                            s.work_date::timestamp AT TIME ZONE 'UTC')
                                            started_at
                                   FROM work_sessions ws
                                   JOIN work_session_segments seg
                                     ON seg.session_id=ws.id
                                   JOIN jira_user_authorization_periods period
                                     ON period.integration_id=s.integration_id
                                    AND period.user_id=s.user_id
                                    AND period.atlassian_account_id
                                        =s.atlassian_account_id
                                   WHERE ws.user_id=s.user_id AND ws.task_id=s.task_id
                                     AND seg.ended_at IS NOT NULL
                                     AND seg.started_at<
                                         (s.work_date::timestamp AT TIME ZONE 'UTC')
                                         +INTERVAL '1 day'
                                     AND seg.started_at<COALESCE(
                                         period.ended_at,'infinity')
                                     AND seg.ended_at>GREATEST(
                                         period.started_at,
                                         s.work_date::timestamp AT TIME ZONE 'UTC')
                                   UNION ALL
                                   SELECT EXTRACT(EPOCH FROM (
                                              LEAST(m.ended_at,
                                                COALESCE(period.ended_at,'infinity'),
                                                (s.work_date::timestamp
                                                 AT TIME ZONE 'UTC')+INTERVAL '1 day')
                                              - GREATEST(m.started_at,period.started_at,
                                                s.work_date::timestamp
                                                AT TIME ZONE 'UTC')
                                          ))::BIGINT,
                                          GREATEST(m.started_at,period.started_at,
                                            s.work_date::timestamp AT TIME ZONE 'UTC')
                                   FROM manual_time_entries m
                                   JOIN jira_user_authorization_periods period
                                     ON period.integration_id=s.integration_id
                                    AND period.user_id=s.user_id
                                    AND period.atlassian_account_id
                                        =s.atlassian_account_id
                                   WHERE m.user_id=s.user_id AND m.task_id=s.task_id
                                     AND m.status='approved'
                                     AND m.started_at<
                                         (s.work_date::timestamp AT TIME ZONE 'UTC')
                                         +INTERVAL '1 day'
                                     AND m.started_at<COALESCE(
                                         period.ended_at,'infinity')
                                     AND m.ended_at>GREATEST(
                                         period.started_at,
                                         s.work_date::timestamp AT TIME ZONE 'UTC')
                               ) entry WHERE entry.seconds>0
                           ) source ON TRUE
                       ), staged AS (
                           INSERT INTO jira_worklog_exports(
                               id,integration_id,user_id,task_id,work_date,
                               atlassian_account_id,desired_seconds,desired_started_at,
                               next_attempt_at,created_at,updated_at
                           )
                           SELECT gen_random_uuid(),t.integration_id,t.user_id,t.task_id,
                                  t.work_date,t.atlassian_account_id,t.desired_seconds,
                                  CASE WHEN t.desired_seconds>0
                                       THEN t.desired_started_at END,
                                  %s,%s,%s
                           FROM totals t
                           WHERE t.desired_seconds>0 OR EXISTS(
                               SELECT 1 FROM jira_worklog_exports existing
                               WHERE existing.integration_id=t.integration_id
                                 AND existing.user_id=t.user_id
                                 AND existing.task_id=t.task_id
                                 AND existing.work_date=t.work_date
                           )
                           ON CONFLICT(integration_id,user_id,task_id,work_date)
                           DO UPDATE SET
                               atlassian_account_id=EXCLUDED.atlassian_account_id,
                               desired_seconds=EXCLUDED.desired_seconds,
                               desired_started_at=EXCLUDED.desired_started_at,
                               next_attempt_at=LEAST(
                                   jira_worklog_exports.next_attempt_at,
                                   EXCLUDED.next_attempt_at
                               ),updated_at=EXCLUDED.updated_at,
                               last_error_code=''
                           WHERE jira_worklog_exports.atlassian_account_id
                                     =EXCLUDED.atlassian_account_id
                             AND (jira_worklog_exports.desired_seconds,
                                  jira_worklog_exports.desired_started_at)
                                 IS DISTINCT FROM
                                 (EXCLUDED.desired_seconds,
                                  EXCLUDED.desired_started_at)
                           RETURNING id
                       ), removed AS (
                           DELETE FROM jira_worklog_dirty_days d USING selected s
                           WHERE d.integration_id=s.integration_id
                             AND d.user_id=s.user_id AND d.task_id=s.task_id
                             AND d.work_date=s.work_date
                           RETURNING d.integration_id
                       )
                       SELECT (SELECT COUNT(*) FROM staged) staged_count,
                              (SELECT COUNT(*) FROM removed) removed_count""",
                    (
                        jira_connection["integration_id"],
                        jira_connection["user_id"],
                        observed_at,
                        observed_at,
                        observed_at,
                        day_limit,
                        observed_at,
                        observed_at,
                        observed_at,
                    ),
                ).fetchone()
                staged_count += int(result["staged_count"])
                remaining = connection.execute(
                    """SELECT EXISTS(
                           SELECT 1 FROM jira_worklog_dirty_days d
                           WHERE d.integration_id=%s AND d.user_id=%s
                             AND (
                               (%s='hourly' AND d.work_date<=%s::timestamptz::date)
                               OR (%s='daily' AND d.work_date<%s::timestamptz::date)
                               OR (%s='delayed'
                                   AND d.work_date<%s::timestamptz::date-1)
                             )
                       ) remaining""",
                    (
                        jira_connection["integration_id"],
                        jira_connection["user_id"],
                        jira_connection["time_sync_mode"],
                        observed_at,
                        jira_connection["time_sync_mode"],
                        observed_at,
                        jira_connection["time_sync_mode"],
                        observed_at,
                    ),
                ).fetchone()["remaining"]
                connection.execute(
                    """UPDATE jira_user_connections
                       SET next_worklog_sync_at=CASE
                             WHEN %s THEN %s
                             WHEN time_sync_mode='hourly' THEN %s+INTERVAL '1 hour'
                             ELSE date_trunc('day',%s::timestamptz)+INTERVAL '1 day'
                           END,updated_at=%s
                       WHERE integration_id=%s AND user_id=%s""",
                    (
                        remaining,
                        observed_at,
                        observed_at,
                        observed_at,
                        observed_at,
                        jira_connection["integration_id"],
                        jira_connection["user_id"],
                    ),
                )
        return staged_count

    def claim_due_jira_worklog_exports(
        self,
        observed_at: datetime,
        claim_token: str,
        *,
        limit: int = 25,
        lease_seconds: int = 180,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100 or not 30 <= lease_seconds <= 600:
            raise ValueError("Invalid Jira worklog claim limit")
        try:
            claim_token = str(uuid.UUID(claim_token))
        except (ValueError, AttributeError) as exc:
            raise ValueError("Invalid Jira worklog claim") from exc
        with self.connect() as connection:
            rows = connection.execute(
                """WITH due AS (
                       SELECT e.id FROM jira_worklog_exports e
                       JOIN integrations i ON i.id=e.integration_id
                       JOIN jira_user_connections c
                         ON c.integration_id=e.integration_id AND c.user_id=e.user_id
                       JOIN users u ON u.id=e.user_id
                       WHERE i.provider='jira' AND i.enabled=TRUE
                         AND c.enabled=TRUE AND c.time_sync_mode<>'off'
                         AND u.enabled=TRUE AND e.next_attempt_at<=%s
                         AND (e.claim_until IS NULL OR e.claim_until<=%s)
                         AND (e.synced_seconds,e.synced_started_at)
                             IS DISTINCT FROM
                             (e.desired_seconds,e.desired_started_at)
                       ORDER BY e.next_attempt_at,e.updated_at,e.id
                       FOR UPDATE OF e SKIP LOCKED LIMIT %s
                   )
                   UPDATE jira_worklog_exports e
                   SET claim_token=%s,claim_until=%s+%s*INTERVAL '1 second',
                       updated_at=%s
                   FROM due,tasks t,integrations i,jira_user_connections c
                   WHERE e.id=due.id AND t.id=e.task_id
                     AND i.id=e.integration_id
                     AND c.integration_id=e.integration_id AND c.user_id=e.user_id
                   RETURNING e.*,t.external_display_key issue_key,
                             i.provider_resource_key cloud_id,
                             c.credential_kind,c.atlassian_account_id connected_account_id""",
                (
                    observed_at,
                    observed_at,
                    limit,
                    claim_token,
                    observed_at,
                    lease_seconds,
                    observed_at,
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_jira_worklog_export_succeeded(
        self,
        export_id: str,
        claim_token: str,
        provider_worklog_id: str | None,
        synced_seconds: int,
        synced_started_at: datetime | None,
        observed_at: datetime,
    ) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE jira_worklog_exports e
                   SET provider_worklog_id=%s,synced_seconds=%s,
                       synced_started_at=%s,attempt_count=0,next_attempt_at=%s,
                       claim_token=NULL,claim_until=NULL,last_error_code='',
                       synced_at=%s,updated_at=%s
                   WHERE e.id=%s AND e.claim_token=%s""",
                (
                    provider_worklog_id,
                    synced_seconds,
                    synced_started_at,
                    observed_at,
                    observed_at,
                    observed_at,
                    export_id,
                    claim_token,
                ),
            )
            if result.rowcount == 1:
                connection.execute(
                    """UPDATE jira_user_connections c
                       SET last_worklog_sync_at=%s,last_worklog_error_code='',
                           updated_at=%s
                       FROM jira_worklog_exports e
                       WHERE e.id=%s AND c.integration_id=e.integration_id
                         AND c.user_id=e.user_id""",
                    (observed_at, observed_at, export_id),
                )
        return result.rowcount == 1

    def renew_jira_worklog_export_claim(
        self,
        export_id: str,
        claim_token: str,
        observed_at: datetime,
        *,
        lease_seconds: int = 600,
    ) -> bool:
        if not 30 <= lease_seconds <= 600:
            raise ValueError("Invalid Jira worklog claim lease")
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE jira_worklog_exports
                   SET claim_until=%s+%s*INTERVAL '1 second',updated_at=%s
                   WHERE id=%s AND claim_token=%s""",
                (
                    observed_at,
                    lease_seconds,
                    observed_at,
                    export_id,
                    claim_token,
                ),
            )
        return result.rowcount == 1

    def mark_jira_worklog_export_failed(
        self,
        export_id: str,
        claim_token: str,
        observed_at: datetime,
        error_code: str,
        retry_seconds: int,
    ) -> bool:
        safe_code = re.sub(r"[^a-z0-9_]", "_", error_code.lower())[:64]
        retry_seconds = min(max(int(retry_seconds), 30), 21_600)
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE jira_worklog_exports e
                   SET attempt_count=attempt_count+1,
                       next_attempt_at=%s+%s*INTERVAL '1 second',
                       claim_token=NULL,claim_until=NULL,last_error_code=%s,
                       updated_at=%s
                   WHERE e.id=%s AND e.claim_token=%s""",
                (
                    observed_at,
                    retry_seconds,
                    safe_code,
                    observed_at,
                    export_id,
                    claim_token,
                ),
            )
            if result.rowcount == 1:
                connection.execute(
                    """UPDATE jira_user_connections c
                       SET last_worklog_error_code=%s,updated_at=%s
                       FROM jira_worklog_exports e
                       WHERE e.id=%s AND c.integration_id=e.integration_id
                         AND c.user_id=e.user_id""",
                    (safe_code, observed_at, export_id),
                )
        return result.rowcount == 1

    def disconnect_jira_user(self, integration_id: str, user_id: str) -> None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT credential_kind FROM jira_user_connections
                   WHERE integration_id=%s AND user_id=%s FOR UPDATE""",
                (integration_id, user_id),
            ).fetchone()
            if not row:
                raise ValueError("Jira user connection not found")
            if row["credential_kind"] == "site":
                raise ValueError(
                    "Disconnect the Jira site to remove its connector account"
                )
            connection.execute(
                """DELETE FROM integration_user_credentials
                   WHERE integration_id=%s AND user_id=%s""",
                (integration_id, user_id),
            )
            connection.execute(
                """UPDATE jira_user_authorization_periods SET ended_at=%s
                   WHERE integration_id=%s AND user_id=%s AND ended_at IS NULL""",
                (utc_now(), integration_id, user_id),
            )
            connection.execute(
                """DELETE FROM jira_user_connections
                   WHERE integration_id=%s AND user_id=%s""",
                (integration_id, user_id),
            )

    def delete_unconfigured_jira_integration(self, integration_id: str) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """DELETE FROM integrations i
                   WHERE i.id=%s AND i.provider='jira'
                     AND NOT EXISTS (
                         SELECT 1 FROM integration_credentials c
                         WHERE c.integration_id=i.id
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM jira_project_mappings m
                         WHERE m.integration_id=i.id
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM tasks t WHERE t.integration_id=i.id
                     )""",
                (integration_id,),
            )
        return result.rowcount == 1

    def delete_integration_credentials(
        self, integration_id: str, provider: str
    ) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """DELETE FROM integration_credentials c USING integrations i
                   WHERE c.integration_id=%s AND i.id=c.integration_id
                     AND i.provider=%s""",
                (integration_id, provider),
            )
        return result.rowcount == 1

    def jira_project_mappings(self, integration_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT m.*,p.name project_name,p.enabled project_enabled,
                          (SELECT COUNT(*) FROM tasks t
                           WHERE t.integration_id=m.integration_id
                             AND t.external_container_key=m.external_project_id)
                          task_count
                   FROM jira_project_mappings m
                   JOIN projects p ON p.id=m.project_id
                   WHERE m.integration_id=%s
                   ORDER BY lower(m.external_project_name),m.external_project_key""",
                (integration_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_jira_project_mapping(
        self,
        integration_id: str,
        external_project_id: str,
        external_project_key: str,
        external_project_name: str,
        project_id: str,
    ) -> None:
        jira_id = external_project_id.strip()
        jira_key = external_project_key.strip()
        jira_name = " ".join(external_project_name.replace("\x00", "").split())
        if (
            not _JIRA_PROJECT_ID.fullmatch(jira_id)
            or not _JIRA_PROJECT_KEY.fullmatch(jira_key)
            or not jira_name
            or len(jira_name) > 255
        ):
            raise ValueError("Choose a valid Jira project")
        now = utc_now()
        try:
            with self.connect() as connection:
                integration = connection.execute(
                    """SELECT id FROM integrations
                       WHERE id=%s AND provider='jira' AND enabled=TRUE""",
                    (integration_id,),
                ).fetchone()
                if not integration:
                    raise ValueError("Jira integration not found")
                project = connection.execute(
                    "SELECT id FROM projects WHERE id=%s AND enabled=TRUE",
                    (project_id,),
                ).fetchone()
                if not project:
                    raise ValueError("Active project not found")
                existing = connection.execute(
                    """SELECT project_id FROM jira_project_mappings
                       WHERE integration_id=%s AND external_project_id=%s""",
                    (integration_id, jira_id),
                ).fetchone()
                if existing and existing["project_id"] != project_id:
                    task_ids = [
                        row["id"]
                        for row in connection.execute(
                            """SELECT id FROM tasks WHERE integration_id=%s
                               AND external_container_key=%s""",
                            (integration_id, jira_id),
                        ).fetchall()
                    ]
                    _close_task_sessions(connection, task_ids, now)
                connection.execute(
                    """INSERT INTO jira_project_mappings(
                           integration_id,external_project_id,external_project_key,
                           external_project_name,project_id,created_at,updated_at
                       ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(integration_id,external_project_id) DO UPDATE SET
                           external_project_key=EXCLUDED.external_project_key,
                           external_project_name=EXCLUDED.external_project_name,
                           project_id=EXCLUDED.project_id,updated_at=EXCLUDED.updated_at""",
                    (
                        integration_id,
                        jira_id,
                        jira_key,
                        jira_name,
                        project_id,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """UPDATE tasks SET project_id=%s
                       WHERE integration_id=%s AND external_container_key=%s""",
                    (project_id, integration_id, jira_id),
                )
                connection.execute(
                    "UPDATE integrations SET next_sync_at=%s,updated_at=%s WHERE id=%s",
                    (now, now, integration_id),
                )
        except UniqueViolation as exc:
            raise ValueError(
                "This project already has a task with the same Jira-derived name"
            ) from exc

    def remove_jira_project_mapping(
        self, integration_id: str, external_project_id: str
    ) -> None:
        with self.connect() as connection:
            task_ids = [
                row["id"]
                for row in connection.execute(
                    """SELECT id FROM tasks WHERE integration_id=%s
                       AND external_container_key=%s""",
                    (integration_id, external_project_id),
                ).fetchall()
            ]
            result = connection.execute(
                """DELETE FROM jira_project_mappings
                   WHERE integration_id=%s AND external_project_id=%s""",
                (integration_id, external_project_id),
            )
            if result.rowcount != 1:
                raise ValueError("Jira project mapping not found")
            _close_task_sessions(connection, task_ids, utc_now())
            connection.execute(
                """UPDATE tasks SET status='archived'
                   WHERE integration_id=%s AND external_container_key=%s""",
                (integration_id, external_project_id),
            )

    def disconnect_jira_integration(self, integration_id: str) -> None:
        observed_at = utc_now()
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE integrations SET enabled=FALSE,updated_at=%s,
                          sync_claim_token=NULL,sync_claim_until=NULL
                   WHERE id=%s AND provider='jira'""",
                (observed_at, integration_id),
            )
            if result.rowcount != 1:
                raise ValueError("Jira integration not found")
            connection.execute(
                "UPDATE tasks SET external_read_only=FALSE WHERE integration_id=%s",
                (integration_id,),
            )
            connection.execute(
                "DELETE FROM integration_user_credentials WHERE integration_id=%s",
                (integration_id,),
            )
            connection.execute(
                """UPDATE jira_user_authorization_periods SET ended_at=%s
                   WHERE integration_id=%s AND ended_at IS NULL""",
                (observed_at, integration_id),
            )
            connection.execute(
                "DELETE FROM jira_user_connections WHERE integration_id=%s",
                (integration_id,),
            )
            connection.execute(
                "DELETE FROM integration_credentials WHERE integration_id=%s",
                (integration_id,),
            )

    def mark_jira_mapping_synced(
        self,
        integration_id: str,
        external_project_id: str,
        observed_at: datetime,
        *,
        full: bool,
    ) -> bool:
        with self.connect() as connection:
            if full:
                result = connection.execute(
                    """UPDATE jira_project_mappings
                       SET last_full_sync_at=%s,last_incremental_sync_at=%s,
                           updated_at=%s
                       WHERE integration_id=%s AND external_project_id=%s""",
                    (
                        observed_at,
                        observed_at,
                        observed_at,
                        integration_id,
                        external_project_id,
                    ),
                )
            else:
                result = connection.execute(
                    """UPDATE jira_project_mappings
                       SET last_incremental_sync_at=%s,updated_at=%s
                       WHERE integration_id=%s AND external_project_id=%s""",
                    (observed_at, observed_at, integration_id, external_project_id),
                )
        return result.rowcount == 1

    def apply_jira_issue(
        self,
        integration_id: str,
        external_project_id: str,
        issue: dict[str, Any],
        observed_at: datetime,
    ) -> str | None:
        external_key = f"jira:{issue['id']}"
        external_updated_at = issue["updated_at"]
        if not isinstance(external_updated_at, datetime):
            raise ValueError("Jira issue update time is invalid")
        with self.connect() as connection:
            mapping = connection.execute(
                """SELECT m.project_id,i.created_by_user_id
                   FROM jira_project_mappings m
                   JOIN integrations i ON i.id=m.integration_id
                   WHERE m.integration_id=%s AND m.external_project_id=%s
                     AND i.provider='jira' AND i.enabled=TRUE
                   FOR SHARE OF m,i""",
                (integration_id, external_project_id),
            ).fetchone()
            if not mapping:
                return None
            existing = connection.execute(
                """SELECT id,external_updated_at FROM tasks
                   WHERE integration_id=%s AND external_key=%s FOR UPDATE""",
                (integration_id, external_key),
            ).fetchone()
            if existing and existing["external_updated_at"]:
                stored_update = existing["external_updated_at"]
                if isinstance(stored_update, str):
                    stored_update = datetime.fromisoformat(stored_update)
                if stored_update > external_updated_at:
                    return existing["id"]
            task_status = "archived" if issue["done"] else "active"
            name = _jira_task_name(str(issue["key"]), str(issue["summary"]))
            description = str(issue.get("description") or "").replace("\x00", "")[
                :10_000
            ]
            if existing:
                if task_status == "archived":
                    _close_task_sessions(connection, [existing["id"]], observed_at)
                connection.execute(
                    """UPDATE tasks SET project_id=%s,name=%s,description=%s,status=%s,
                              external_container_key=%s,external_url=%s,
                              external_display_key=%s,
                              external_updated_at=%s,external_read_only=TRUE,
                              external_observed_at=clock_timestamp(),
                              external_missing_since=NULL
                       WHERE id=%s""",
                    (
                        mapping["project_id"],
                        name,
                        description,
                        task_status,
                        external_project_id,
                        str(issue["url"])[:1000],
                        str(issue["key"]),
                        external_updated_at,
                        existing["id"],
                    ),
                )
                return existing["id"]
            task_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO tasks(
                       id,project_id,name,description,status,billable,created_at,
                       created_by_user_id,integration_id,external_container_key,
                       external_key,external_url,external_display_key,
                       external_updated_at,external_read_only,external_observed_at
                   ) VALUES (%s,%s,%s,%s,%s,TRUE,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,
                             clock_timestamp())""",
                (
                    task_id,
                    mapping["project_id"],
                    name,
                    description,
                    task_status,
                    observed_at,
                    mapping["created_by_user_id"],
                    integration_id,
                    external_project_id,
                    external_key,
                    str(issue["url"])[:1000],
                    str(issue["key"]),
                    external_updated_at,
                ),
            )
            return task_id

    def archive_missing_jira_issues(
        self,
        integration_id: str,
        external_project_id: str,
        seen_keys: list[str],
        snapshot_at: datetime,
    ) -> int:
        with self.connect() as connection:
            if seen_keys:
                connection.execute(
                    """UPDATE tasks SET external_missing_since=NULL
                       WHERE integration_id=%s AND external_container_key=%s
                         AND external_key=ANY(%s::text[])""",
                    (integration_id, external_project_id, seen_keys),
                )
                missing_filter = "AND NOT (external_key=ANY(%s::text[]))"
                missing_parameters: tuple[Any, ...] = (seen_keys,)
            else:
                missing_filter = ""
                missing_parameters = ()
            rows = connection.execute(
                f"""UPDATE tasks SET status='archived'
                   WHERE integration_id=%s AND external_container_key=%s
                     {missing_filter}
                     AND external_missing_since IS NOT NULL
                     AND external_missing_since < %s
                     AND (external_observed_at IS NULL OR external_observed_at <= %s)
                   RETURNING id""",
                (
                    integration_id,
                    external_project_id,
                    *missing_parameters,
                    snapshot_at,
                    snapshot_at,
                ),
            ).fetchall()
            connection.execute(
                f"""UPDATE tasks SET external_missing_since=%s
                   WHERE integration_id=%s AND external_container_key=%s
                     {missing_filter}
                     AND external_missing_since IS NULL
                     AND (external_observed_at IS NULL OR external_observed_at <= %s)""",
                (
                    snapshot_at,
                    integration_id,
                    external_project_id,
                    *missing_parameters,
                    snapshot_at,
                ),
            )
            task_ids = [row["id"] for row in rows]
            _close_task_sessions(connection, task_ids, utc_now())
        return len(task_ids)

    def claim_jira_integration(
        self,
        integration_id: str,
        claim_token: str,
        observed_at: datetime,
        *,
        lease_seconds: int = 300,
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """UPDATE integrations SET sync_claim_token=%s,sync_claim_until=%s
                   WHERE id=%s AND provider='jira' AND enabled=TRUE
                     AND (sync_claim_until IS NULL OR sync_claim_until < %s)
                   RETURNING *""",
                (
                    claim_token,
                    observed_at + timedelta(seconds=lease_seconds),
                    integration_id,
                    observed_at,
                ),
            ).fetchone()
        return dict(row) if row else None

    def claim_due_jira_integrations(
        self,
        observed_at: datetime,
        claim_token: str,
        *,
        lease_seconds: int = 300,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("Invalid Jira integration claim limit")
        with self.connect() as connection:
            rows = connection.execute(
                """WITH candidates AS (
                       SELECT id FROM integrations
                       WHERE provider='jira' AND enabled=TRUE AND next_sync_at <= %s
                         AND (sync_claim_until IS NULL OR sync_claim_until < %s)
                       ORDER BY next_sync_at,id FOR UPDATE SKIP LOCKED LIMIT %s
                   )
                   UPDATE integrations i SET sync_claim_token=%s,sync_claim_until=%s
                   FROM candidates WHERE i.id=candidates.id RETURNING i.*""",
                (
                    observed_at,
                    observed_at,
                    limit,
                    claim_token,
                    observed_at + timedelta(seconds=lease_seconds),
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def renew_jira_sync_claim(
        self,
        integration_id: str,
        claim_token: str,
        observed_at: datetime,
        *,
        lease_seconds: int = 300,
    ) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE integrations SET sync_claim_until=%s,updated_at=%s
                   WHERE id=%s AND provider='jira' AND enabled=TRUE
                     AND sync_claim_token=%s""",
                (
                    observed_at + timedelta(seconds=lease_seconds),
                    observed_at,
                    integration_id,
                    claim_token,
                ),
            )
        return result.rowcount == 1

    def mark_jira_sync_succeeded(
        self,
        integration_id: str,
        claim_token: str,
        observed_at: datetime,
        *,
        interval_seconds: int = 300,
    ) -> None:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE integrations SET last_sync_at=%s,next_sync_at=%s,
                          last_error_code='',consecutive_failures=0,
                          sync_claim_token=NULL,sync_claim_until=NULL,updated_at=%s
                   WHERE id=%s AND sync_claim_token=%s""",
                (
                    observed_at,
                    observed_at + timedelta(seconds=interval_seconds),
                    observed_at,
                    integration_id,
                    claim_token,
                ),
            )
            if result.rowcount != 1:
                raise ValueError("Jira integration sync claim was lost")

    def mark_jira_sync_failed(
        self,
        integration_id: str,
        claim_token: str,
        observed_at: datetime,
        error_code: str,
        *,
        retry_seconds: int,
    ) -> None:
        if not re.fullmatch(r"[a-z0-9_]{1,80}", error_code):
            raise ValueError("Invalid Jira sync error code")
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE integrations SET next_sync_at=%s,last_error_code=%s,
                          consecutive_failures=LEAST(consecutive_failures+1,1000000),
                          sync_claim_token=NULL,sync_claim_until=NULL,updated_at=%s
                   WHERE id=%s AND sync_claim_token=%s""",
                (
                    observed_at + timedelta(seconds=retry_seconds),
                    error_code,
                    observed_at,
                    integration_id,
                    claim_token,
                ),
            )
            if result.rowcount != 1:
                raise ValueError("Jira integration sync claim was lost")

    def github_sync_snapshot_at(self) -> datetime:
        return self.integration_sync_snapshot_at()

    def upsert_github_installation(
        self,
        installation_id: int,
        account_login: str,
        account_type: str,
        created_by_user_id: str,
    ) -> str:
        login = account_login.strip()
        kind = account_type.strip()
        if installation_id <= 0 or not login or len(login) > 255:
            raise ValueError("GitHub returned an invalid installation")
        if kind not in {"User", "Organization", "Enterprise"}:
            raise ValueError("GitHub returned an invalid installation account")
        integration_id = str(uuid.uuid4())
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """INSERT INTO integrations(
                       id,provider,display_name,enabled,provider_external_id,
                       account_login,account_type,created_by_user_id,created_at,
                       updated_at,next_sync_at
                   ) VALUES (%s,'github',%s,TRUE,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(provider,provider_external_id)
                     WHERE provider='github' AND provider_external_id IS NOT NULL
                   DO UPDATE SET display_name=EXCLUDED.display_name,
                     account_login=EXCLUDED.account_login,
                     account_type=EXCLUDED.account_type,enabled=TRUE,
                     created_by_user_id=EXCLUDED.created_by_user_id,
                     updated_at=EXCLUDED.updated_at,next_sync_at=EXCLUDED.next_sync_at,
                     last_error_code='',consecutive_failures=0,
                     sync_claim_token=NULL,sync_claim_until=NULL
                   RETURNING id""",
                (
                    integration_id,
                    f"GitHub · {login}"[:120],
                    installation_id,
                    login,
                    kind,
                    created_by_user_id,
                    now,
                    now,
                    now,
                ),
            ).fetchone()
        return row["id"]

    def list_github_integrations(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT i.*,
                       (SELECT COUNT(*) FROM integration_project_mappings m
                        WHERE m.integration_id=i.id) mapping_count,
                       (SELECT COUNT(*) FROM tasks t
                        WHERE t.integration_id=i.id) synced_task_count
                   FROM integrations i WHERE i.provider='github'
                   ORDER BY i.enabled DESC,lower(i.account_login)"""
            ).fetchall()
        return [dict(row) for row in rows]

    def get_github_integration(self, integration_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT i.*,
                       (SELECT COUNT(*) FROM integration_project_mappings m
                        WHERE m.integration_id=i.id) mapping_count,
                       (SELECT COUNT(*) FROM tasks t
                        WHERE t.integration_id=i.id) synced_task_count
                   FROM integrations i
                   WHERE i.id=%s AND i.provider='github'""",
                (integration_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_github_integration_by_installation(
        self, installation_id: int
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM integrations
                   WHERE provider='github' AND provider_external_id=%s""",
                (installation_id,),
            ).fetchone()
        return dict(row) if row else None

    def github_project_mappings(self, integration_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT m.*,p.name project_name,p.enabled project_enabled,
                          (SELECT COUNT(*) FROM tasks t
                           WHERE t.integration_id=m.integration_id
                             AND t.external_repository_id=m.external_repository_id)
                          task_count
                   FROM integration_project_mappings m
                   JOIN projects p ON p.id=m.project_id
                   WHERE m.integration_id=%s
                   ORDER BY lower(m.external_repository_name)""",
                (integration_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_github_project_mapping(
        self,
        integration_id: str,
        repository_id: int,
        repository_name: str,
        project_id: str,
    ) -> None:
        repository = repository_name.strip()
        if repository_id <= 0 or not _REPOSITORY_NAME.fullmatch(repository):
            raise ValueError("Choose a valid GitHub repository")
        now = utc_now()
        try:
            with self.connect() as connection:
                integration = connection.execute(
                    """SELECT id FROM integrations
                       WHERE id=%s AND provider='github' AND enabled=TRUE""",
                    (integration_id,),
                ).fetchone()
                if not integration:
                    raise ValueError("GitHub integration not found")
                project = connection.execute(
                    "SELECT id FROM projects WHERE id=%s AND enabled=TRUE",
                    (project_id,),
                ).fetchone()
                if not project:
                    raise ValueError("Active project not found")
                existing_mapping = connection.execute(
                    """SELECT project_id FROM integration_project_mappings
                       WHERE integration_id=%s AND external_repository_id=%s""",
                    (integration_id, repository_id),
                ).fetchone()
                if existing_mapping and existing_mapping["project_id"] != project_id:
                    task_ids = [
                        row["id"]
                        for row in connection.execute(
                            """SELECT id FROM tasks
                               WHERE integration_id=%s
                                 AND external_repository_id=%s""",
                            (integration_id, repository_id),
                        ).fetchall()
                    ]
                    _close_task_sessions(connection, task_ids, now)
                connection.execute(
                    """INSERT INTO integration_project_mappings(
                           integration_id,external_repository_id,
                           external_repository_name,project_id,created_at,updated_at
                       ) VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(integration_id,external_repository_id)
                       DO UPDATE SET external_repository_name=EXCLUDED.external_repository_name,
                                     project_id=EXCLUDED.project_id,
                                     updated_at=EXCLUDED.updated_at""",
                    (
                        integration_id,
                        repository_id,
                        repository[:255],
                        project_id,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """UPDATE tasks SET project_id=%s
                       WHERE integration_id=%s AND external_repository_id=%s""",
                    (project_id, integration_id, repository_id),
                )
                connection.execute(
                    """UPDATE integrations SET next_sync_at=%s,updated_at=%s
                       WHERE id=%s""",
                    (now, now, integration_id),
                )
        except UniqueViolation as exc:
            raise ValueError(
                "This project already has a task with the same GitHub-derived name"
            ) from exc

    def mark_github_mapping_synced(
        self,
        integration_id: str,
        repository_id: int,
        observed_at: datetime,
        *,
        full: bool,
    ) -> bool:
        with self.connect() as connection:
            if full:
                result = connection.execute(
                    """UPDATE integration_project_mappings
                       SET last_full_sync_at=%s,last_incremental_sync_at=%s,
                           updated_at=%s
                       WHERE integration_id=%s AND external_repository_id=%s""",
                    (
                        observed_at,
                        observed_at,
                        observed_at,
                        integration_id,
                        repository_id,
                    ),
                )
            else:
                result = connection.execute(
                    """UPDATE integration_project_mappings
                       SET last_incremental_sync_at=%s,updated_at=%s
                       WHERE integration_id=%s AND external_repository_id=%s""",
                    (observed_at, observed_at, integration_id, repository_id),
                )
        return result.rowcount == 1

    def remove_github_project_mapping(
        self, integration_id: str, repository_id: int
    ) -> None:
        with self.connect() as connection:
            task_ids = [
                row["id"]
                for row in connection.execute(
                    """SELECT id FROM tasks
                       WHERE integration_id=%s AND external_repository_id=%s""",
                    (integration_id, repository_id),
                ).fetchall()
            ]
            result = connection.execute(
                """DELETE FROM integration_project_mappings
                   WHERE integration_id=%s AND external_repository_id=%s""",
                (integration_id, repository_id),
            )
            if result.rowcount != 1:
                raise ValueError("GitHub repository mapping not found")
            _close_task_sessions(connection, task_ids, utc_now())
            connection.execute(
                """UPDATE tasks SET status='archived'
                   WHERE integration_id=%s AND external_repository_id=%s""",
                (integration_id, repository_id),
            )

    def disconnect_github_integration(self, integration_id: str) -> None:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE integrations SET enabled=FALSE,updated_at=%s,
                          sync_claim_token=NULL,sync_claim_until=NULL
                   WHERE id=%s AND provider='github'""",
                (utc_now(), integration_id),
            )
            if result.rowcount != 1:
                raise ValueError("GitHub integration not found")
            connection.execute(
                "UPDATE tasks SET external_read_only=FALSE WHERE integration_id=%s",
                (integration_id,),
            )

    def claim_github_integration(
        self,
        integration_id: str,
        claim_token: str,
        observed_at: datetime,
        *,
        lease_seconds: int = 300,
    ) -> dict[str, Any] | None:
        claim_until = observed_at + timedelta(seconds=lease_seconds)
        with self.connect() as connection:
            row = connection.execute(
                """UPDATE integrations SET sync_claim_token=%s,sync_claim_until=%s
                   WHERE id=%s AND provider='github' AND enabled=TRUE
                     AND (sync_claim_until IS NULL OR sync_claim_until < %s)
                   RETURNING *""",
                (claim_token, claim_until, integration_id, observed_at),
            ).fetchone()
        return dict(row) if row else None

    def claim_due_github_integrations(
        self,
        observed_at: datetime,
        claim_token: str,
        *,
        lease_seconds: int = 300,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("Invalid GitHub integration claim limit")
        claim_until = observed_at + timedelta(seconds=lease_seconds)
        with self.connect() as connection:
            rows = connection.execute(
                """WITH candidates AS (
                       SELECT id FROM integrations
                       WHERE provider='github' AND enabled=TRUE
                         AND next_sync_at <= %s
                         AND (sync_claim_until IS NULL OR sync_claim_until < %s)
                       ORDER BY next_sync_at,id
                       FOR UPDATE SKIP LOCKED LIMIT %s
                   )
                   UPDATE integrations i
                   SET sync_claim_token=%s,sync_claim_until=%s
                   FROM candidates WHERE i.id=candidates.id
                   RETURNING i.*""",
                (observed_at, observed_at, limit, claim_token, claim_until),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_github_sync_succeeded(
        self,
        integration_id: str,
        claim_token: str,
        observed_at: datetime,
        *,
        interval_seconds: int = 300,
    ) -> None:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE integrations SET last_sync_at=%s,next_sync_at=%s,
                          last_error_code='',consecutive_failures=0,
                          sync_claim_token=NULL,sync_claim_until=NULL,updated_at=%s
                   WHERE id=%s AND sync_claim_token=%s""",
                (
                    observed_at,
                    observed_at + timedelta(seconds=interval_seconds),
                    observed_at,
                    integration_id,
                    claim_token,
                ),
            )
            if result.rowcount != 1:
                raise ValueError("GitHub integration sync claim was lost")

    def mark_github_sync_failed(
        self,
        integration_id: str,
        claim_token: str,
        observed_at: datetime,
        error_code: str,
        *,
        retry_seconds: int,
    ) -> None:
        if not re.fullmatch(r"[a-z0-9_]{1,80}", error_code):
            raise ValueError("Invalid GitHub sync error code")
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE integrations SET next_sync_at=%s,last_error_code=%s,
                          consecutive_failures=LEAST(consecutive_failures+1,1000000),
                          sync_claim_token=NULL,sync_claim_until=NULL,updated_at=%s
                   WHERE id=%s AND sync_claim_token=%s""",
                (
                    observed_at + timedelta(seconds=retry_seconds),
                    error_code,
                    observed_at,
                    integration_id,
                    claim_token,
                ),
            )
            if result.rowcount != 1:
                raise ValueError("GitHub integration sync claim was lost")

    def apply_github_issue(
        self,
        integration_id: str,
        repository_id: int,
        repository_name: str,
        issue: dict[str, Any],
        observed_at: datetime,
    ) -> str | None:
        external_key = str(issue["node_id"])
        number = int(issue["number"])
        external_updated_at = issue["updated_at"]
        if not isinstance(external_updated_at, datetime):
            raise ValueError("GitHub issue update time is invalid")
        with self.connect() as connection:
            mapping = connection.execute(
                """SELECT m.project_id,i.created_by_user_id
                   FROM integration_project_mappings m
                   JOIN integrations i ON i.id=m.integration_id
                   WHERE m.integration_id=%s AND m.external_repository_id=%s
                     AND i.provider='github' AND i.enabled=TRUE
                   FOR SHARE OF m,i""",
                (integration_id, repository_id),
            ).fetchone()
            if not mapping:
                return None
            existing = connection.execute(
                """SELECT id,external_updated_at FROM tasks
                   WHERE integration_id=%s AND external_key=%s FOR UPDATE""",
                (integration_id, external_key),
            ).fetchone()
            if existing and existing["external_updated_at"]:
                stored_update = existing["external_updated_at"]
                if isinstance(stored_update, str):
                    stored_update = datetime.fromisoformat(stored_update)
                if stored_update > external_updated_at:
                    return existing["id"]
            name = _github_task_name(repository_name, number, str(issue["title"]))
            description = str(issue.get("body") or "").replace("\x00", "")[:10_000]
            task_status = "active" if issue["state"] == "open" else "archived"
            if existing:
                if task_status == "archived":
                    _close_task_sessions(connection, [existing["id"]], observed_at)
                connection.execute(
                    """UPDATE tasks SET project_id=%s,name=%s,description=%s,status=%s,
                              external_repository_id=%s,external_url=%s,
                              external_updated_at=%s,external_read_only=TRUE,
                              external_observed_at=clock_timestamp()
                       WHERE id=%s""",
                    (
                        mapping["project_id"],
                        name,
                        description,
                        task_status,
                        repository_id,
                        str(issue["html_url"])[:1000],
                        external_updated_at,
                        existing["id"],
                    ),
                )
                return existing["id"]
            task_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO tasks(
                       id,project_id,name,description,status,billable,created_at,
                       created_by_user_id,integration_id,external_repository_id,
                       external_key,external_url,external_updated_at,external_read_only,
                       external_observed_at
                   ) VALUES (%s,%s,%s,%s,%s,TRUE,%s,%s,%s,%s,%s,%s,%s,TRUE,
                             clock_timestamp())""",
                (
                    task_id,
                    mapping["project_id"],
                    name,
                    description,
                    task_status,
                    observed_at,
                    mapping["created_by_user_id"],
                    integration_id,
                    repository_id,
                    external_key[:500],
                    str(issue["html_url"])[:1000],
                    external_updated_at,
                ),
            )
            return task_id

    def archive_missing_github_issues(
        self,
        integration_id: str,
        repository_id: int,
        seen_keys: list[str],
        snapshot_at: datetime,
    ) -> int:
        with self.connect() as connection:
            if seen_keys:
                rows = connection.execute(
                    """UPDATE tasks SET status='archived'
                       WHERE integration_id=%s AND external_repository_id=%s
                         AND NOT (external_key=ANY(%s::text[]))
                         AND (external_observed_at IS NULL OR external_observed_at <= %s)
                       RETURNING id""",
                    (integration_id, repository_id, seen_keys, snapshot_at),
                ).fetchall()
            else:
                rows = connection.execute(
                    """UPDATE tasks SET status='archived'
                       WHERE integration_id=%s AND external_repository_id=%s
                         AND (external_observed_at IS NULL OR external_observed_at <= %s)
                       RETURNING id""",
                    (integration_id, repository_id, snapshot_at),
                ).fetchall()
            task_ids = [row["id"] for row in rows]
            _close_task_sessions(connection, task_ids, utc_now())
        return len(task_ids)

    def begin_integration_webhook(
        self,
        provider: str,
        delivery_id: str,
        event_name: str,
        observed_at: datetime,
    ) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """INSERT INTO integration_webhook_deliveries(
                       provider,delivery_id,event_name,received_at
                   ) VALUES (%s,%s,%s,%s)
                   ON CONFLICT(provider,delivery_id) DO UPDATE
                     SET event_name=EXCLUDED.event_name,
                         received_at=EXCLUDED.received_at,outcome='received',
                         processed_at=NULL
                   WHERE integration_webhook_deliveries.outcome='failed'
                      OR (integration_webhook_deliveries.outcome='received'
                          AND integration_webhook_deliveries.received_at < %s)
                   RETURNING delivery_id""",
                (
                    provider,
                    delivery_id,
                    event_name,
                    observed_at,
                    observed_at - timedelta(minutes=10),
                ),
            ).fetchone()
        return row is not None

    def finish_integration_webhook(
        self, provider: str, delivery_id: str, outcome: str
    ) -> None:
        if outcome not in {"processed", "ignored", "failed"}:
            raise ValueError("Invalid integration webhook outcome")
        with self.connect() as connection:
            connection.execute(
                """UPDATE integration_webhook_deliveries
                   SET outcome=%s,processed_at=%s
                   WHERE provider=%s AND delivery_id=%s""",
                (outcome, utc_now(), provider, delivery_id),
            )

    def purge_integration_webhooks(self, cutoff: datetime, limit: int = 1000) -> int:
        with self.connect() as connection:
            result = connection.execute(
                """DELETE FROM integration_webhook_deliveries
                   WHERE (provider,delivery_id) IN (
                       SELECT provider,delivery_id FROM integration_webhook_deliveries
                       WHERE received_at < %s ORDER BY received_at LIMIT %s
                   )""",
                (cutoff, min(max(limit, 1), 10_000)),
            )
        return result.rowcount

    def upsert_asana_workspace(
        self,
        workspace_gid: str,
        workspace_name: str,
        created_by_user_id: str,
    ) -> str:
        gid = workspace_gid.strip()
        name = " ".join(workspace_name.replace("\x00", "").split())
        if not _ASANA_GID.fullmatch(gid) or not name or len(name) > 255:
            raise ValueError("Asana returned an invalid workspace")
        integration_id = str(uuid.uuid4())
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """INSERT INTO integrations(
                       id,provider,display_name,enabled,provider_resource_key,
                       provider_resource_url,account_login,account_type,
                       created_by_user_id,created_at,updated_at,next_sync_at
                   ) VALUES (%s,'asana',%s,TRUE,%s,'',%s,'Workspace',%s,%s,%s,%s)
                   ON CONFLICT(provider,provider_resource_key)
                     WHERE provider='asana' AND provider_resource_key<>''
                   DO UPDATE SET display_name=EXCLUDED.display_name,
                     account_login=EXCLUDED.account_login,enabled=TRUE,
                     created_by_user_id=EXCLUDED.created_by_user_id,
                     updated_at=EXCLUDED.updated_at,next_sync_at=EXCLUDED.next_sync_at,
                     last_error_code='',consecutive_failures=0,
                     sync_claim_token=NULL,sync_claim_until=NULL
                   RETURNING id""",
                (
                    integration_id,
                    f"Asana · {name}"[:120],
                    gid,
                    name,
                    created_by_user_id,
                    now,
                    now,
                    now,
                ),
            ).fetchone()
            connection.execute(
                """UPDATE asana_project_mappings
                   SET last_full_sync_at=NULL,updated_at=%s,
                       sync_claim_token=NULL,sync_claim_until=NULL
                   WHERE integration_id=%s""",
                (now, row["id"]),
            )
        return str(row["id"])

    def list_asana_integrations(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT i.*,
                          (SELECT COUNT(*) FROM asana_project_mappings m
                           WHERE m.integration_id=i.id) mapping_count,
                          (SELECT COUNT(*) FROM tasks t
                           WHERE t.integration_id=i.id) synced_task_count
                   FROM integrations i WHERE i.provider='asana'
                   ORDER BY i.enabled DESC,lower(i.account_login)"""
            ).fetchall()
        return [dict(row) for row in rows]

    def get_asana_integration(self, integration_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT i.*,
                          (SELECT COUNT(*) FROM asana_project_mappings m
                           WHERE m.integration_id=i.id) mapping_count,
                          (SELECT COUNT(*) FROM tasks t
                           WHERE t.integration_id=i.id) synced_task_count
                   FROM integrations i
                   WHERE i.id=%s AND i.provider='asana'""",
                (integration_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_asana_integration_by_resource(
        self, workspace_gid: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM integrations
                   WHERE provider='asana' AND provider_resource_key=%s""",
                (workspace_gid,),
            ).fetchone()
        return dict(row) if row else None

    def upsert_asana_user_connection(
        self,
        integration_id: str,
        user_id: str,
        asana_user_gid: str,
        display_name: str,
        credential_kind: str,
    ) -> dict[str, Any]:
        gid = asana_user_gid.strip()
        name = " ".join(display_name.replace("\x00", "").split())
        if (
            not _ASANA_GID.fullmatch(gid)
            or not name
            or len(name) > 255
            or credential_kind not in {"site", "member"}
        ):
            raise ValueError("Asana returned an invalid user identity")
        now = utc_now()
        try:
            with self.connect() as connection:
                authorization = connection.execute(
                    """SELECT i.created_by_user_id,u.enabled,
                              EXISTS(SELECT 1 FROM integration_credentials c
                                     WHERE c.integration_id=i.id) site_credential,
                              EXISTS(SELECT 1 FROM integration_user_credentials c
                                     WHERE c.integration_id=i.id
                                       AND c.user_id=u.id) member_credential
                       FROM integrations i JOIN users u ON u.id=%s
                       WHERE i.id=%s AND i.provider='asana' AND i.enabled=TRUE
                       FOR SHARE OF i,u""",
                    (user_id, integration_id),
                ).fetchone()
                if not authorization or not authorization["enabled"]:
                    raise ValueError("Asana integration user is unavailable")
                prior_export_identity = connection.execute(
                    """SELECT asana_user_gid FROM asana_comment_exports
                       WHERE integration_id=%s AND user_id=%s
                         AND asana_user_gid<>%s LIMIT 1""",
                    (integration_id, user_id, gid),
                ).fetchone()
                if prior_export_identity:
                    raise ValueError(
                        "Reconnect the same Asana account that owns this user's "
                        "existing comments"
                    )
                if credential_kind == "site":
                    if authorization["created_by_user_id"] != user_id:
                        raise ValueError(
                            "Only the Asana workspace connector can use this grant"
                        )
                    if not authorization["site_credential"]:
                        raise ValueError("Asana workspace authorization is unavailable")
                    connection.execute(
                        """UPDATE asana_user_connections
                           SET enabled=FALSE,updated_at=%s
                           WHERE integration_id=%s AND credential_kind='site'
                             AND user_id<>%s""",
                        (now, integration_id, user_id),
                    )
                    connection.execute(
                        """DELETE FROM integration_user_credentials
                           WHERE integration_id=%s AND user_id=%s""",
                        (integration_id, user_id),
                    )
                elif not authorization["member_credential"]:
                    raise ValueError("Asana member authorization is unavailable")
                row = connection.execute(
                    """INSERT INTO asana_user_connections(
                           integration_id,user_id,asana_user_gid,display_name,
                           credential_kind,time_sync_mode,enabled,connected_at,updated_at,
                           next_comment_sync_at,last_comment_error_code
                       ) VALUES (%s,%s,%s,%s,%s,'daily',TRUE,%s,%s,%s,'')
                       ON CONFLICT(integration_id,user_id) DO UPDATE SET
                           asana_user_gid=EXCLUDED.asana_user_gid,
                           display_name=EXCLUDED.display_name,
                           credential_kind=EXCLUDED.credential_kind,enabled=TRUE,
                           connected_at=EXCLUDED.connected_at,
                           updated_at=EXCLUDED.updated_at,
                           next_comment_sync_at=EXCLUDED.next_comment_sync_at,
                           last_comment_error_code=''
                       RETURNING *""",
                    (
                        integration_id,
                        user_id,
                        gid,
                        name,
                        credential_kind,
                        now,
                        now,
                        now,
                    ),
                ).fetchone()
                open_period = connection.execute(
                    """SELECT id,asana_user_gid
                       FROM asana_user_authorization_periods
                       WHERE integration_id=%s AND user_id=%s AND ended_at IS NULL
                       FOR UPDATE""",
                    (integration_id, user_id),
                ).fetchone()
                if open_period and open_period["asana_user_gid"] != gid:
                    connection.execute(
                        """UPDATE asana_user_authorization_periods
                           SET ended_at=%s WHERE id=%s""",
                        (now, open_period["id"]),
                    )
                    open_period = None
                if not open_period:
                    connection.execute(
                        """INSERT INTO asana_user_authorization_periods(
                               id,integration_id,user_id,asana_user_gid,
                               started_at,created_at
                           ) VALUES (%s,%s,%s,%s,%s,%s)""",
                        (str(uuid.uuid4()), integration_id, user_id, gid, now, now),
                    )
        except UniqueViolation as exc:
            raise ValueError(
                "That Asana account is already connected to another Dayfinch user"
            ) from exc
        return dict(row)

    def asana_user_connection(
        self, integration_id: str, user_id: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT c.*,u.email,u.full_name,u.enabled user_enabled,
                          i.enabled integration_enabled
                   FROM asana_user_connections c
                   JOIN users u ON u.id=c.user_id
                   JOIN integrations i ON i.id=c.integration_id
                   WHERE c.integration_id=%s AND c.user_id=%s
                     AND i.provider='asana'""",
                (integration_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def asana_comment_identity_conflicts(
        self, integration_id: str, user_id: str, asana_user_gid: str
    ) -> bool:
        gid = asana_user_gid.strip()
        if not _ASANA_GID.fullmatch(gid):
            return True
        with self.connect() as connection:
            row = connection.execute(
                """SELECT 1 FROM asana_comment_exports
                   WHERE integration_id=%s AND user_id=%s
                     AND asana_user_gid<>%s LIMIT 1""",
                (integration_id, user_id, gid),
            ).fetchone()
        return row is not None

    def list_asana_user_connections(self, integration_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT c.*,u.email,u.full_name,u.enabled user_enabled
                   FROM asana_user_connections c JOIN users u ON u.id=c.user_id
                   WHERE c.integration_id=%s
                   ORDER BY c.enabled DESC,lower(COALESCE(u.full_name,u.email))""",
                (integration_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_asana_user_sync_mode(
        self, integration_id: str, user_id: str, sync_mode: str
    ) -> None:
        if sync_mode not in {"off", "hourly", "daily", "delayed", "completed"}:
            raise ValueError("Choose a valid Asana time synchronization mode")
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE asana_user_connections
                   SET time_sync_mode=%s,updated_at=%s,next_comment_sync_at=%s,
                       last_comment_error_code=''
                   WHERE integration_id=%s AND user_id=%s AND enabled=TRUE""",
                (sync_mode, utc_now(), utc_now(), integration_id, user_id),
            )
            if result.rowcount != 1:
                raise ValueError("Asana user connection not found")

    def disconnect_asana_user(self, integration_id: str, user_id: str) -> None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT credential_kind FROM asana_user_connections
                   WHERE integration_id=%s AND user_id=%s FOR UPDATE""",
                (integration_id, user_id),
            ).fetchone()
            if not row:
                raise ValueError("Asana user connection not found")
            if row["credential_kind"] == "site":
                raise ValueError(
                    "Disconnect the Asana workspace to remove its connector account"
                )
            connection.execute(
                """DELETE FROM integration_user_credentials
                   WHERE integration_id=%s AND user_id=%s""",
                (integration_id, user_id),
            )
            connection.execute(
                """UPDATE asana_user_authorization_periods SET ended_at=%s
                   WHERE integration_id=%s AND user_id=%s AND ended_at IS NULL""",
                (utc_now(), integration_id, user_id),
            )
            connection.execute(
                """DELETE FROM asana_user_connections
                   WHERE integration_id=%s AND user_id=%s""",
                (integration_id, user_id),
            )

    def asana_project_mappings(self, integration_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT m.*,p.name project_name,p.enabled project_enabled,
                          (SELECT COUNT(*) FROM tasks t
                           WHERE t.integration_id=m.integration_id
                             AND t.external_container_key=m.external_project_id)
                          task_count
                   FROM asana_project_mappings m
                   JOIN projects p ON p.id=m.project_id
                   WHERE m.integration_id=%s
                   ORDER BY lower(m.external_project_name),m.external_project_id""",
                (integration_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_asana_project_mapping(
        self,
        integration_id: str,
        external_project_id: str,
        external_project_name: str,
        project_id: str,
    ) -> None:
        gid = external_project_id.strip()
        name = " ".join(external_project_name.replace("\x00", "").split())
        if not _ASANA_GID.fullmatch(gid) or not name or len(name) > 255:
            raise ValueError("Choose a valid Asana project")
        now = utc_now()
        try:
            with self.connect() as connection:
                integration = connection.execute(
                    """SELECT id FROM integrations
                       WHERE id=%s AND provider='asana' AND enabled=TRUE""",
                    (integration_id,),
                ).fetchone()
                project = connection.execute(
                    "SELECT id FROM projects WHERE id=%s AND enabled=TRUE",
                    (project_id,),
                ).fetchone()
                if not integration:
                    raise ValueError("Asana integration not found")
                if not project:
                    raise ValueError("Active project not found")
                existing = connection.execute(
                    """SELECT project_id FROM asana_project_mappings
                       WHERE integration_id=%s AND external_project_id=%s""",
                    (integration_id, gid),
                ).fetchone()
                if existing and existing["project_id"] != project_id:
                    task_ids = [
                        row["id"]
                        for row in connection.execute(
                            """SELECT id FROM tasks WHERE integration_id=%s
                               AND external_container_key=%s""",
                            (integration_id, gid),
                        ).fetchall()
                    ]
                    _close_task_sessions(connection, task_ids, now)
                connection.execute(
                    """INSERT INTO asana_project_mappings(
                           integration_id,external_project_id,external_project_name,
                           project_id,created_at,updated_at
                       ) VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(integration_id,external_project_id) DO UPDATE SET
                           external_project_name=EXCLUDED.external_project_name,
                           project_id=EXCLUDED.project_id,last_full_sync_at=NULL,
                           sync_claim_token=NULL,sync_claim_until=NULL,
                           updated_at=EXCLUDED.updated_at""",
                    (integration_id, gid, name, project_id, now, now),
                )
                connection.execute(
                    """UPDATE tasks SET project_id=%s
                       WHERE integration_id=%s AND external_container_key=%s""",
                    (project_id, integration_id, gid),
                )
                connection.execute(
                    "UPDATE integrations SET next_sync_at=%s,updated_at=%s WHERE id=%s",
                    (now, now, integration_id),
                )
        except UniqueViolation as exc:
            raise ValueError(
                "This project already has a task with the same Asana-derived name"
            ) from exc

    def remove_asana_project_mapping(
        self, integration_id: str, external_project_id: str
    ) -> None:
        with self.connect() as connection:
            task_ids = [
                row["id"]
                for row in connection.execute(
                    """SELECT id FROM tasks WHERE integration_id=%s
                       AND external_container_key=%s""",
                    (integration_id, external_project_id),
                ).fetchall()
            ]
            result = connection.execute(
                """DELETE FROM asana_project_mappings
                   WHERE integration_id=%s AND external_project_id=%s""",
                (integration_id, external_project_id),
            )
            if result.rowcount != 1:
                raise ValueError("Asana project mapping not found")
            _close_task_sessions(connection, task_ids, utc_now())
            connection.execute(
                """UPDATE tasks SET status='archived'
                   WHERE integration_id=%s AND external_container_key=%s""",
                (integration_id, external_project_id),
            )

    def mark_asana_mapping_synced(
        self,
        integration_id: str,
        external_project_id: str,
        claim_token: str,
        observed_at: datetime,
    ) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE asana_project_mappings
                   SET last_full_sync_at=%s,sync_claim_token=NULL,
                       sync_claim_until=NULL,updated_at=%s
                   WHERE integration_id=%s AND external_project_id=%s
                     AND sync_claim_token=%s""",
                (
                    observed_at,
                    observed_at,
                    integration_id,
                    external_project_id,
                    claim_token,
                ),
            )
        return result.rowcount == 1

    def claim_asana_project_mappings(
        self,
        integration_id: str,
        claim_token: str,
        observed_at: datetime,
        *,
        limit: int = 10,
        lease_seconds: int = 600,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 50 or not 60 <= lease_seconds <= 1800:
            raise ValueError("Invalid Asana mapping claim limit")
        with self.connect() as connection:
            rows = connection.execute(
                """WITH candidates AS (
                       SELECT m.integration_id,m.external_project_id
                       FROM asana_project_mappings m
                       JOIN integrations i ON i.id=m.integration_id
                       WHERE m.integration_id=%s AND i.provider='asana'
                         AND i.enabled=TRUE AND i.sync_claim_token=%s
                         AND (
                           m.last_full_sync_at IS NULL
                           OR (i.last_sync_at IS NOT NULL
                               AND m.last_full_sync_at<=i.last_sync_at)
                         )
                         AND (m.sync_claim_until IS NULL OR m.sync_claim_until<%s)
                       ORDER BY m.last_full_sync_at NULLS FIRST,m.updated_at,
                                m.external_project_id
                       FOR UPDATE OF m SKIP LOCKED LIMIT %s
                   )
                   UPDATE asana_project_mappings m
                   SET sync_claim_token=%s,
                       sync_claim_until=%s+%s*INTERVAL '1 second',updated_at=%s
                   FROM candidates c,projects p
                   WHERE m.integration_id=c.integration_id
                     AND m.external_project_id=c.external_project_id
                     AND p.id=m.project_id
                   RETURNING m.*,p.name project_name,p.enabled project_enabled,
                     (SELECT COUNT(*) FROM tasks t
                      WHERE t.integration_id=m.integration_id
                        AND t.external_container_key=m.external_project_id) task_count""",
                (
                    integration_id,
                    claim_token,
                    observed_at,
                    limit,
                    claim_token,
                    observed_at,
                    lease_seconds,
                    observed_at,
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def renew_asana_mapping_claim(
        self,
        integration_id: str,
        external_project_id: str,
        claim_token: str,
        observed_at: datetime,
        *,
        lease_seconds: int = 600,
    ) -> bool:
        if not 60 <= lease_seconds <= 1800:
            raise ValueError("Invalid Asana mapping claim lease")
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE asana_project_mappings
                   SET sync_claim_until=%s+%s*INTERVAL '1 second',updated_at=%s
                   WHERE integration_id=%s AND external_project_id=%s
                     AND sync_claim_token=%s""",
                (
                    observed_at,
                    lease_seconds,
                    observed_at,
                    integration_id,
                    external_project_id,
                    claim_token,
                ),
            )
        return result.rowcount == 1

    def release_asana_mapping_claims(
        self, integration_id: str, claim_token: str, observed_at: datetime
    ) -> int:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE asana_project_mappings
                   SET sync_claim_token=NULL,sync_claim_until=NULL,updated_at=%s
                   WHERE integration_id=%s AND sync_claim_token=%s""",
                (observed_at, integration_id, claim_token),
            )
        return result.rowcount

    def has_due_asana_project_mappings(
        self, integration_id: str, claim_token: str
    ) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT EXISTS(
                       SELECT 1 FROM asana_project_mappings m
                       JOIN integrations i ON i.id=m.integration_id
                       WHERE m.integration_id=%s AND i.provider='asana'
                         AND i.enabled=TRUE AND i.sync_claim_token=%s
                         AND (
                           m.last_full_sync_at IS NULL
                           OR (i.last_sync_at IS NOT NULL
                               AND m.last_full_sync_at<=i.last_sync_at)
                         )
                   ) pending""",
                (integration_id, claim_token),
            ).fetchone()
        return bool(row and row["pending"])

    def apply_asana_task(
        self,
        integration_id: str,
        external_project_id: str,
        task: dict[str, Any],
        observed_at: datetime,
    ) -> str | None:
        task_gid = str(task["gid"])
        external_key = f"asana:{external_project_id}:{task_gid}"
        external_updated_at = task["modified_at"]
        if (
            not _ASANA_GID.fullmatch(task_gid)
            or len(external_key) > 500
            or not isinstance(external_updated_at, datetime)
        ):
            raise ValueError("Asana task identity is invalid")
        with self.connect() as connection:
            mapping = connection.execute(
                """SELECT m.project_id,i.created_by_user_id
                   FROM asana_project_mappings m
                   JOIN integrations i ON i.id=m.integration_id
                   WHERE m.integration_id=%s AND m.external_project_id=%s
                     AND i.provider='asana' AND i.enabled=TRUE
                   FOR SHARE OF m,i""",
                (integration_id, external_project_id),
            ).fetchone()
            if not mapping:
                return None
            existing = connection.execute(
                """SELECT id,external_updated_at FROM tasks
                   WHERE integration_id=%s AND external_key=%s FOR UPDATE""",
                (integration_id, external_key),
            ).fetchone()
            if existing and existing["external_updated_at"]:
                stored_update = existing["external_updated_at"]
                if isinstance(stored_update, str):
                    stored_update = datetime.fromisoformat(stored_update)
                if stored_update > external_updated_at:
                    return existing["id"]
            task_status = "archived" if task["completed"] else "active"
            name = _asana_task_name(task_gid, str(task["name"]))
            description = str(task.get("notes") or "").replace("\x00", "")[:10_000]
            if existing:
                if task_status == "archived":
                    _close_task_sessions(connection, [existing["id"]], observed_at)
                connection.execute(
                    """UPDATE tasks SET project_id=%s,name=%s,description=%s,status=%s,
                              external_container_key=%s,external_url=%s,
                              external_display_key=%s,external_updated_at=%s,
                              external_read_only=TRUE,
                              external_observed_at=clock_timestamp(),
                              external_missing_since=NULL
                       WHERE id=%s""",
                    (
                        mapping["project_id"],
                        name,
                        description,
                        task_status,
                        external_project_id,
                        str(task.get("url") or "")[:1000],
                        task_gid,
                        external_updated_at,
                        existing["id"],
                    ),
                )
                task_id = existing["id"]
            else:
                task_id = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO tasks(
                           id,project_id,name,description,status,billable,created_at,
                           created_by_user_id,integration_id,external_container_key,
                           external_key,external_url,external_display_key,
                           external_updated_at,external_read_only,external_observed_at
                       ) VALUES (%s,%s,%s,%s,%s,TRUE,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,
                                 clock_timestamp())""",
                    (
                        task_id,
                        mapping["project_id"],
                        name,
                        description,
                        task_status,
                        observed_at,
                        mapping["created_by_user_id"],
                        integration_id,
                        external_project_id,
                        external_key,
                        str(task.get("url") or "")[:1000],
                        task_gid,
                        external_updated_at,
                    ),
                )
            assignee_gid = str(task.get("assignee_gid") or "")
            if assignee_gid and not _ASANA_GID.fullmatch(assignee_gid):
                raise ValueError("Asana task assignee is invalid")
            connection.execute(
                """INSERT INTO asana_task_assignees(
                       integration_id,external_project_id,external_task_gid,
                       asana_user_gid,observed_at
                   ) VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT(integration_id,external_project_id,external_task_gid)
                   DO UPDATE SET asana_user_gid=EXCLUDED.asana_user_gid,
                                 observed_at=EXCLUDED.observed_at""",
                (
                    integration_id,
                    external_project_id,
                    task_gid,
                    assignee_gid,
                    observed_at,
                ),
            )
        return str(task_id)

    def reconcile_asana_project_tasks(
        self,
        integration_id: str,
        external_project_id: str,
        seen_keys: list[str],
        snapshot_at: datetime,
    ) -> int:
        with self.connect() as connection:
            if seen_keys:
                connection.execute(
                    """UPDATE tasks SET external_missing_since=NULL
                       WHERE integration_id=%s AND external_container_key=%s
                         AND external_key=ANY(%s::text[])""",
                    (integration_id, external_project_id, seen_keys),
                )
                missing_filter = "AND NOT (external_key=ANY(%s::text[]))"
                missing_parameters: tuple[Any, ...] = (seen_keys,)
            else:
                missing_filter = ""
                missing_parameters = ()
            rows = connection.execute(
                f"""UPDATE tasks SET status='archived'
                   WHERE integration_id=%s AND external_container_key=%s
                     {missing_filter}
                     AND external_missing_since IS NOT NULL
                     AND external_missing_since < %s
                     AND (external_observed_at IS NULL OR external_observed_at <= %s)
                   RETURNING id""",
                (
                    integration_id,
                    external_project_id,
                    *missing_parameters,
                    snapshot_at,
                    snapshot_at,
                ),
            ).fetchall()
            connection.execute(
                f"""UPDATE tasks SET external_missing_since=%s
                   WHERE integration_id=%s AND external_container_key=%s
                     {missing_filter}
                     AND external_missing_since IS NULL
                     AND (external_observed_at IS NULL OR external_observed_at <= %s)""",
                (
                    snapshot_at,
                    integration_id,
                    external_project_id,
                    *missing_parameters,
                    snapshot_at,
                ),
            )
            connection.execute(
                """DELETE FROM asana_task_assignees
                   WHERE integration_id=%s AND external_project_id=%s
                     AND observed_at < %s""",
                (integration_id, external_project_id, snapshot_at),
            )
            task_ids = [row["id"] for row in rows]
            _close_task_sessions(connection, task_ids, utc_now())
        return len(task_ids)

    def claim_asana_integration(
        self,
        integration_id: str,
        claim_token: str,
        observed_at: datetime,
        *,
        lease_seconds: int = 600,
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """UPDATE integrations SET sync_claim_token=%s,sync_claim_until=%s
                   WHERE id=%s AND provider='asana' AND enabled=TRUE
                     AND (sync_claim_until IS NULL OR sync_claim_until < %s)
                   RETURNING *""",
                (
                    claim_token,
                    observed_at + timedelta(seconds=lease_seconds),
                    integration_id,
                    observed_at,
                ),
            ).fetchone()
        return dict(row) if row else None

    def claim_due_asana_integrations(
        self,
        observed_at: datetime,
        claim_token: str,
        *,
        lease_seconds: int = 600,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 25:
            raise ValueError("Invalid Asana integration claim limit")
        with self.connect() as connection:
            rows = connection.execute(
                """WITH candidates AS (
                       SELECT id FROM integrations
                       WHERE provider='asana' AND enabled=TRUE AND next_sync_at<=%s
                         AND (sync_claim_until IS NULL OR sync_claim_until<%s)
                       ORDER BY next_sync_at,id FOR UPDATE SKIP LOCKED LIMIT %s
                   )
                   UPDATE integrations i SET sync_claim_token=%s,sync_claim_until=%s
                   FROM candidates WHERE i.id=candidates.id RETURNING i.*""",
                (
                    observed_at,
                    observed_at,
                    limit,
                    claim_token,
                    observed_at + timedelta(seconds=lease_seconds),
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def renew_asana_sync_claim(
        self,
        integration_id: str,
        claim_token: str,
        observed_at: datetime,
        *,
        lease_seconds: int = 600,
    ) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE integrations SET sync_claim_until=%s,updated_at=%s
                   WHERE id=%s AND provider='asana' AND enabled=TRUE
                     AND sync_claim_token=%s""",
                (
                    observed_at + timedelta(seconds=lease_seconds),
                    observed_at,
                    integration_id,
                    claim_token,
                ),
            )
        return result.rowcount == 1

    def mark_asana_sync_succeeded(
        self,
        integration_id: str,
        claim_token: str,
        observed_at: datetime,
        *,
        interval_seconds: int = 300,
    ) -> None:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE integrations SET last_sync_at=%s,next_sync_at=%s,
                          last_error_code='',consecutive_failures=0,
                          sync_claim_token=NULL,sync_claim_until=NULL,updated_at=%s
                   WHERE id=%s AND sync_claim_token=%s""",
                (
                    observed_at,
                    observed_at + timedelta(seconds=interval_seconds),
                    observed_at,
                    integration_id,
                    claim_token,
                ),
            )
            if result.rowcount != 1:
                raise ValueError("Asana integration sync claim was lost")

    def mark_asana_sync_partial(
        self,
        integration_id: str,
        claim_token: str,
        observed_at: datetime,
    ) -> None:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE integrations SET next_sync_at=%s,
                          sync_claim_token=NULL,sync_claim_until=NULL,updated_at=%s
                   WHERE id=%s AND provider='asana' AND sync_claim_token=%s""",
                (observed_at, observed_at, integration_id, claim_token),
            )
            if result.rowcount != 1:
                raise ValueError("Asana integration sync claim was lost")

    def mark_asana_sync_failed(
        self,
        integration_id: str,
        claim_token: str,
        observed_at: datetime,
        error_code: str,
        *,
        retry_seconds: int,
    ) -> None:
        if not re.fullmatch(r"[a-z0-9_]{1,80}", error_code):
            raise ValueError("Invalid Asana sync error code")
        retry_seconds = min(max(int(retry_seconds), 30), 21_600)
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE integrations SET next_sync_at=%s,last_error_code=%s,
                          consecutive_failures=LEAST(consecutive_failures+1,1000000),
                          sync_claim_token=NULL,sync_claim_until=NULL,updated_at=%s
                   WHERE id=%s AND sync_claim_token=%s""",
                (
                    observed_at + timedelta(seconds=retry_seconds),
                    error_code,
                    observed_at,
                    integration_id,
                    claim_token,
                ),
            )
            if result.rowcount != 1:
                raise ValueError("Asana integration sync claim was lost")

    def delete_unconfigured_asana_integration(self, integration_id: str) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """DELETE FROM integrations i
                   WHERE i.id=%s AND i.provider='asana'
                     AND NOT EXISTS(SELECT 1 FROM integration_credentials c
                                    WHERE c.integration_id=i.id)
                     AND NOT EXISTS(SELECT 1 FROM asana_project_mappings m
                                    WHERE m.integration_id=i.id)
                     AND NOT EXISTS(SELECT 1 FROM tasks t
                                    WHERE t.integration_id=i.id)""",
                (integration_id,),
            )
        return result.rowcount == 1

    def disconnect_asana_integration(self, integration_id: str) -> None:
        observed_at = utc_now()
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE integrations SET enabled=FALSE,updated_at=%s,
                          sync_claim_token=NULL,sync_claim_until=NULL
                   WHERE id=%s AND provider='asana'""",
                (observed_at, integration_id),
            )
            if result.rowcount != 1:
                raise ValueError("Asana integration not found")
            connection.execute(
                "UPDATE tasks SET external_read_only=FALSE WHERE integration_id=%s",
                (integration_id,),
            )
            connection.execute(
                "DELETE FROM integration_user_credentials WHERE integration_id=%s",
                (integration_id,),
            )
            connection.execute(
                """UPDATE asana_user_authorization_periods SET ended_at=%s
                   WHERE integration_id=%s AND ended_at IS NULL""",
                (observed_at, integration_id),
            )
            connection.execute(
                "DELETE FROM asana_user_connections WHERE integration_id=%s",
                (integration_id,),
            )
            connection.execute(
                "DELETE FROM integration_credentials WHERE integration_id=%s",
                (integration_id,),
            )

    def stage_due_asana_comments(
        self,
        observed_at: datetime,
        *,
        connection_limit: int = 10,
        day_limit: int = 250,
    ) -> int:
        if observed_at.tzinfo is None:
            raise ValueError("Asana comment observation time must include a timezone")
        if not 1 <= connection_limit <= 100 or not 1 <= day_limit <= 1000:
            raise ValueError("Invalid Asana comment staging limit")
        staged_count = 0
        with self.connect() as connection:
            connections = connection.execute(
                """SELECT c.* FROM asana_user_connections c
                   JOIN integrations i ON i.id=c.integration_id
                   JOIN users u ON u.id=c.user_id
                   WHERE c.enabled=TRUE AND c.time_sync_mode<>'off'
                     AND c.next_comment_sync_at<=%s
                     AND i.provider='asana' AND i.enabled=TRUE AND u.enabled=TRUE
                   ORDER BY c.next_comment_sync_at,c.integration_id,c.user_id
                   FOR UPDATE OF c SKIP LOCKED LIMIT %s""",
                (observed_at, connection_limit),
            ).fetchall()
            for asana_connection in connections:
                result = connection.execute(
                    """WITH selected AS (
                           SELECT d.*,c.asana_user_gid
                           FROM asana_comment_dirty_days d
                           JOIN asana_user_connections c
                             ON c.integration_id=d.integration_id
                            AND c.user_id=d.user_id
                           WHERE d.integration_id=%s AND d.user_id=%s
                             AND (
                               (c.time_sync_mode='hourly'
                                AND d.work_date<=%s::timestamptz::date)
                               OR (c.time_sync_mode='daily'
                                   AND d.work_date<%s::timestamptz::date)
                               OR (c.time_sync_mode='delayed'
                                   AND d.work_date<%s::timestamptz::date-1)
                               OR (c.time_sync_mode='completed' AND NOT EXISTS(
                                   SELECT 1 FROM tasks active_task
                                   WHERE active_task.integration_id=d.integration_id
                                     AND active_task.external_display_key
                                         =d.external_task_gid
                                     AND active_task.status='active'
                               ))
                             )
                           ORDER BY d.work_date,d.changed_at,d.external_task_gid
                           FOR UPDATE OF d SKIP LOCKED LIMIT %s
                       ), totals AS (
                           SELECT s.*,
                                  LEAST(COALESCE(source.seconds,0),2147483647)::BIGINT
                                      desired_seconds,
                                  source.started_at desired_started_at
                           FROM selected s
                           LEFT JOIN LATERAL (
                               SELECT SUM(entry.seconds)::BIGINT seconds,
                                      MIN(entry.started_at) started_at
                               FROM (
                                   SELECT EXTRACT(EPOCH FROM (
                                              LEAST(seg.ended_at,
                                                COALESCE(period.ended_at,'infinity'),
                                                (s.work_date::timestamp
                                                 AT TIME ZONE 'UTC')+INTERVAL '1 day')
                                              - GREATEST(seg.started_at,period.started_at,
                                                s.work_date::timestamp
                                                AT TIME ZONE 'UTC')
                                          ))::BIGINT seconds,
                                          GREATEST(seg.started_at,period.started_at,
                                            s.work_date::timestamp AT TIME ZONE 'UTC')
                                            started_at
                                   FROM work_sessions ws
                                   JOIN work_session_segments seg
                                     ON seg.session_id=ws.id
                                   JOIN tasks provider_task ON provider_task.id=ws.task_id
                                   JOIN asana_user_authorization_periods period
                                     ON period.integration_id=s.integration_id
                                    AND period.user_id=s.user_id
                                    AND period.asana_user_gid=s.asana_user_gid
                                   WHERE ws.user_id=s.user_id
                                     AND provider_task.integration_id=s.integration_id
                                     AND provider_task.external_display_key
                                         =s.external_task_gid
                                     AND seg.ended_at IS NOT NULL
                                     AND seg.started_at<
                                         (s.work_date::timestamp AT TIME ZONE 'UTC')
                                         +INTERVAL '1 day'
                                     AND seg.started_at<COALESCE(
                                         period.ended_at,'infinity')
                                     AND seg.ended_at>GREATEST(
                                         period.started_at,
                                         s.work_date::timestamp AT TIME ZONE 'UTC')
                                   UNION ALL
                                   SELECT EXTRACT(EPOCH FROM (
                                              LEAST(m.ended_at,
                                                COALESCE(period.ended_at,'infinity'),
                                                (s.work_date::timestamp
                                                 AT TIME ZONE 'UTC')+INTERVAL '1 day')
                                              - GREATEST(m.started_at,period.started_at,
                                                s.work_date::timestamp
                                                AT TIME ZONE 'UTC')
                                          ))::BIGINT,
                                          GREATEST(m.started_at,period.started_at,
                                            s.work_date::timestamp AT TIME ZONE 'UTC')
                                   FROM manual_time_entries m
                                   JOIN tasks provider_task ON provider_task.id=m.task_id
                                   JOIN asana_user_authorization_periods period
                                     ON period.integration_id=s.integration_id
                                    AND period.user_id=s.user_id
                                    AND period.asana_user_gid=s.asana_user_gid
                                   WHERE m.user_id=s.user_id AND m.status='approved'
                                     AND provider_task.integration_id=s.integration_id
                                     AND provider_task.external_display_key
                                         =s.external_task_gid
                                     AND m.started_at<
                                         (s.work_date::timestamp AT TIME ZONE 'UTC')
                                         +INTERVAL '1 day'
                                     AND m.started_at<COALESCE(
                                         period.ended_at,'infinity')
                                     AND m.ended_at>GREATEST(
                                         period.started_at,
                                         s.work_date::timestamp AT TIME ZONE 'UTC')
                               ) entry WHERE entry.seconds>0
                           ) source ON TRUE
                       ), staged AS (
                           INSERT INTO asana_comment_exports(
                               id,integration_id,user_id,external_task_gid,work_date,
                               asana_user_gid,desired_seconds,desired_started_at,
                               next_attempt_at,created_at,updated_at
                           )
                           SELECT gen_random_uuid(),t.integration_id,t.user_id,
                                  t.external_task_gid,t.work_date,t.asana_user_gid,
                                  t.desired_seconds,
                                  CASE WHEN t.desired_seconds>0
                                       THEN t.desired_started_at END,
                                  %s,%s,%s
                           FROM totals t
                           WHERE t.desired_seconds>0 OR EXISTS(
                               SELECT 1 FROM asana_comment_exports existing
                               WHERE existing.integration_id=t.integration_id
                                 AND existing.user_id=t.user_id
                                 AND existing.external_task_gid=t.external_task_gid
                                 AND existing.work_date=t.work_date
                           )
                           ON CONFLICT(
                               integration_id,user_id,external_task_gid,work_date
                           ) DO UPDATE SET
                               asana_user_gid=EXCLUDED.asana_user_gid,
                               desired_seconds=EXCLUDED.desired_seconds,
                               desired_started_at=EXCLUDED.desired_started_at,
                               next_attempt_at=LEAST(
                                   asana_comment_exports.next_attempt_at,
                                   EXCLUDED.next_attempt_at
                               ),updated_at=EXCLUDED.updated_at,
                               last_error_code=''
                           WHERE asana_comment_exports.asana_user_gid
                                     =EXCLUDED.asana_user_gid
                             AND (asana_comment_exports.desired_seconds,
                                  asana_comment_exports.desired_started_at)
                                 IS DISTINCT FROM
                                 (EXCLUDED.desired_seconds,
                                  EXCLUDED.desired_started_at)
                           RETURNING id
                       ), removed AS (
                           DELETE FROM asana_comment_dirty_days d USING selected s
                           WHERE d.integration_id=s.integration_id
                             AND d.user_id=s.user_id
                             AND d.external_task_gid=s.external_task_gid
                             AND d.work_date=s.work_date
                           RETURNING d.integration_id
                       )
                       SELECT (SELECT COUNT(*) FROM staged) staged_count,
                              (SELECT COUNT(*) FROM removed) removed_count""",
                    (
                        asana_connection["integration_id"],
                        asana_connection["user_id"],
                        observed_at,
                        observed_at,
                        observed_at,
                        day_limit,
                        observed_at,
                        observed_at,
                        observed_at,
                    ),
                ).fetchone()
                staged_count += int(result["staged_count"])
                remaining = connection.execute(
                    """SELECT EXISTS(
                           SELECT 1 FROM asana_comment_dirty_days d
                           WHERE d.integration_id=%s AND d.user_id=%s
                             AND (
                               (%s='hourly' AND d.work_date<=%s::timestamptz::date)
                               OR (%s='daily' AND d.work_date<%s::timestamptz::date)
                               OR (%s='delayed'
                                   AND d.work_date<%s::timestamptz::date-1)
                               OR (%s='completed' AND NOT EXISTS(
                                   SELECT 1 FROM tasks active_task
                                   WHERE active_task.integration_id=d.integration_id
                                     AND active_task.external_display_key
                                         =d.external_task_gid
                                     AND active_task.status='active'
                               ))
                             )
                       ) remaining""",
                    (
                        asana_connection["integration_id"],
                        asana_connection["user_id"],
                        asana_connection["time_sync_mode"],
                        observed_at,
                        asana_connection["time_sync_mode"],
                        observed_at,
                        asana_connection["time_sync_mode"],
                        observed_at,
                        asana_connection["time_sync_mode"],
                    ),
                ).fetchone()["remaining"]
                connection.execute(
                    """UPDATE asana_user_connections
                       SET next_comment_sync_at=CASE
                             WHEN %s THEN %s
                             WHEN time_sync_mode='hourly' THEN %s+INTERVAL '1 hour'
                             WHEN time_sync_mode='completed' THEN %s+INTERVAL '5 minutes'
                             ELSE date_trunc('day',%s::timestamptz)+INTERVAL '1 day'
                           END,updated_at=%s
                       WHERE integration_id=%s AND user_id=%s""",
                    (
                        remaining,
                        observed_at,
                        observed_at,
                        observed_at,
                        observed_at,
                        observed_at,
                        asana_connection["integration_id"],
                        asana_connection["user_id"],
                    ),
                )
        return staged_count

    def claim_due_asana_comment_exports(
        self,
        observed_at: datetime,
        claim_token: str,
        *,
        limit: int = 25,
        lease_seconds: int = 180,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100 or not 30 <= lease_seconds <= 600:
            raise ValueError("Invalid Asana comment claim limit")
        try:
            claim_token = str(uuid.UUID(claim_token))
        except (ValueError, AttributeError) as exc:
            raise ValueError("Invalid Asana comment claim") from exc
        with self.connect() as connection:
            rows = connection.execute(
                """WITH due AS (
                       SELECT e.id FROM asana_comment_exports e
                       JOIN integrations i ON i.id=e.integration_id
                       JOIN asana_user_connections c
                         ON c.integration_id=e.integration_id AND c.user_id=e.user_id
                       JOIN users u ON u.id=e.user_id
                       WHERE i.provider='asana' AND i.enabled=TRUE
                         AND c.enabled=TRUE AND c.time_sync_mode<>'off'
                         AND u.enabled=TRUE AND e.next_attempt_at<=%s
                         AND (e.claim_until IS NULL OR e.claim_until<=%s)
                         AND (e.synced_seconds,e.synced_started_at)
                             IS DISTINCT FROM
                             (e.desired_seconds,e.desired_started_at)
                       ORDER BY e.next_attempt_at,e.updated_at,e.id
                       FOR UPDATE OF e SKIP LOCKED LIMIT %s
                   )
                   UPDATE asana_comment_exports e
                   SET claim_token=%s,claim_until=%s+%s*INTERVAL '1 second',
                       updated_at=%s
                   FROM due,integrations i,asana_user_connections c
                   WHERE e.id=due.id AND i.id=e.integration_id
                     AND c.integration_id=e.integration_id AND c.user_id=e.user_id
                   RETURNING e.*,i.provider_resource_key workspace_gid,
                             c.credential_kind,c.asana_user_gid connected_user_gid""",
                (
                    observed_at,
                    observed_at,
                    limit,
                    claim_token,
                    observed_at,
                    lease_seconds,
                    observed_at,
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def renew_asana_comment_export_claim(
        self,
        export_id: str,
        claim_token: str,
        observed_at: datetime,
        *,
        lease_seconds: int = 600,
    ) -> bool:
        if not 30 <= lease_seconds <= 600:
            raise ValueError("Invalid Asana comment claim lease")
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE asana_comment_exports
                   SET claim_until=%s+%s*INTERVAL '1 second',updated_at=%s
                   WHERE id=%s AND claim_token=%s""",
                (observed_at, lease_seconds, observed_at, export_id, claim_token),
            )
        return result.rowcount == 1

    def mark_asana_comment_export_succeeded(
        self,
        export_id: str,
        claim_token: str,
        provider_story_gid: str | None,
        synced_seconds: int,
        synced_started_at: datetime | None,
        observed_at: datetime,
    ) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE asana_comment_exports e
                   SET provider_story_gid=%s,synced_seconds=%s,
                       synced_started_at=%s,attempt_count=0,next_attempt_at=%s,
                       claim_token=NULL,claim_until=NULL,last_error_code='',
                       synced_at=%s,updated_at=%s
                   WHERE e.id=%s AND e.claim_token=%s""",
                (
                    provider_story_gid,
                    synced_seconds,
                    synced_started_at,
                    observed_at,
                    observed_at,
                    observed_at,
                    export_id,
                    claim_token,
                ),
            )
            if result.rowcount == 1:
                connection.execute(
                    """UPDATE asana_user_connections c
                       SET last_comment_sync_at=%s,last_comment_error_code='',
                           updated_at=%s
                       FROM asana_comment_exports e
                       WHERE e.id=%s AND c.integration_id=e.integration_id
                         AND c.user_id=e.user_id""",
                    (observed_at, observed_at, export_id),
                )
        return result.rowcount == 1

    def mark_asana_comment_export_failed(
        self,
        export_id: str,
        claim_token: str,
        observed_at: datetime,
        error_code: str,
        retry_seconds: int,
    ) -> bool:
        safe_code = re.sub(r"[^a-z0-9_]", "_", error_code.lower())[:64]
        retry_seconds = min(max(int(retry_seconds), 30), 21_600)
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE asana_comment_exports e
                   SET attempt_count=attempt_count+1,
                       next_attempt_at=%s+%s*INTERVAL '1 second',
                       claim_token=NULL,claim_until=NULL,last_error_code=%s,
                       updated_at=%s
                   WHERE e.id=%s AND e.claim_token=%s""",
                (
                    observed_at,
                    retry_seconds,
                    safe_code,
                    observed_at,
                    export_id,
                    claim_token,
                ),
            )
            if result.rowcount == 1:
                connection.execute(
                    """UPDATE asana_user_connections c
                       SET last_comment_error_code=%s,updated_at=%s
                       FROM asana_comment_exports e
                       WHERE e.id=%s AND c.integration_id=e.integration_id
                         AND c.user_id=e.user_id""",
                    (safe_code, observed_at, export_id),
                )
        return result.rowcount == 1

    def upsert_slack_workspace(
        self,
        team_id: str,
        team_name: str,
        bot_user_id: str,
        created_by_user_id: str,
    ) -> str:
        team_id = team_id.strip()
        bot_user_id = bot_user_id.strip()
        name = " ".join(team_name.replace("\x00", "").split())
        if (
            not _SLACK_ID.fullmatch(team_id)
            or not _SLACK_ID.fullmatch(bot_user_id)
            or not name
            or len(name) > 255
        ):
            raise ValueError("Slack returned an invalid workspace identity")
        integration_id = str(uuid.uuid4())
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """INSERT INTO integrations(
                       id,provider,display_name,enabled,provider_resource_key,
                       account_login,account_type,created_by_user_id,
                       created_at,updated_at,next_sync_at
                   ) VALUES (%s,'slack',%s,TRUE,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(provider,provider_resource_key)
                     WHERE provider='slack' AND provider_resource_key<>''
                   DO UPDATE SET display_name=EXCLUDED.display_name,enabled=TRUE,
                     account_login=EXCLUDED.account_login,
                     account_type=EXCLUDED.account_type,
                     created_by_user_id=EXCLUDED.created_by_user_id,
                     updated_at=EXCLUDED.updated_at,last_error_code='',
                     consecutive_failures=0
                   RETURNING id""",
                (
                    integration_id,
                    f"Slack · {name}"[:120],
                    team_id,
                    name,
                    bot_user_id,
                    created_by_user_id,
                    now,
                    now,
                    now,
                ),
            ).fetchone()
            connection.execute(
                """INSERT INTO slack_notification_defaults(
                       integration_id,timer_events,todo_events,updated_at
                   ) VALUES (%s,TRUE,TRUE,%s)
                   ON CONFLICT(integration_id) DO NOTHING""",
                (row["id"], now),
            )
        return str(row["id"])

    def get_slack_integration(self, integration_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT i.*,d.timer_events,d.todo_events,
                          (SELECT COUNT(*) FROM slack_destinations destination
                           WHERE destination.integration_id=i.id
                             AND destination.enabled=TRUE) destination_count,
                          (SELECT COUNT(*) FROM slack_outbox message
                           WHERE message.integration_id=i.id
                             AND message.sent_at IS NULL
                             AND message.discarded_at IS NULL) pending_count,
                          (SELECT COUNT(*) FROM slack_outbox message
                           WHERE message.integration_id=i.id
                             AND message.discarded_at IS NOT NULL) discarded_count,
                          (SELECT message.last_error_code FROM slack_outbox message
                           WHERE message.integration_id=i.id
                             AND message.last_error_code<>''
                           ORDER BY message.updated_at DESC,message.id LIMIT 1)
                            delivery_error_code
                   FROM integrations i
                   JOIN slack_notification_defaults d ON d.integration_id=i.id
                   WHERE i.id=%s AND i.provider='slack'""",
                (integration_id,),
            ).fetchone()
        return dict(row) if row else None

    def restore_slack_workspace_after_failed_authorization(
        self, integration_id: str, previous: dict[str, Any]
    ) -> None:
        """Undo the metadata/enable portion of a failed Slack reconnection."""
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE integrations SET display_name=%s,enabled=%s,
                          account_login=%s,account_type=%s,
                          created_by_user_id=%s,updated_at=%s,
                          last_error_code=%s,consecutive_failures=%s
                   WHERE id=%s AND provider='slack'""",
                (
                    previous["display_name"],
                    previous["enabled"],
                    previous["account_login"],
                    previous["account_type"],
                    previous["created_by_user_id"],
                    utc_now(),
                    previous["last_error_code"],
                    previous["consecutive_failures"],
                    integration_id,
                ),
            )
            if result.rowcount != 1:
                raise ValueError("Slack integration not found")

    def list_slack_integrations(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT i.*,
                          (SELECT COUNT(*) FROM slack_destinations d
                           WHERE d.integration_id=i.id AND d.enabled=TRUE)
                          destination_count
                   FROM integrations i WHERE i.provider='slack'
                   ORDER BY i.enabled DESC,lower(i.account_login),i.id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def slack_destinations(self, integration_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM slack_destinations
                   WHERE integration_id=%s
                   ORDER BY enabled DESC,target_kind,lower(display_name),id""",
                (integration_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_slack_destination(
        self,
        integration_id: str,
        slack_target_id: str,
        target_kind: str,
        display_name: str,
    ) -> str:
        target_id = slack_target_id.strip()
        name = " ".join(display_name.replace("\x00", "").split())
        if (
            not _SLACK_ID.fullmatch(target_id)
            or target_kind not in {"channel", "user"}
            or not name
            or len(name) > 255
        ):
            raise ValueError("Choose a valid Slack destination")
        now = utc_now()
        destination_id = str(uuid.uuid4())
        with self.connect() as connection:
            integration = connection.execute(
                """SELECT id FROM integrations
                   WHERE id=%s AND provider='slack' AND enabled=TRUE""",
                (integration_id,),
            ).fetchone()
            if not integration:
                raise ValueError("Slack integration not found")
            row = connection.execute(
                """INSERT INTO slack_destinations(
                       id,integration_id,slack_target_id,target_kind,display_name,
                       enabled,created_at,updated_at
                   ) VALUES (%s,%s,%s,%s,%s,TRUE,%s,%s)
                   ON CONFLICT(integration_id,slack_target_id) DO UPDATE SET
                       target_kind=EXCLUDED.target_kind,
                       display_name=EXCLUDED.display_name,enabled=TRUE,
                       updated_at=EXCLUDED.updated_at
                   RETURNING id""",
                (
                    destination_id,
                    integration_id,
                    target_id,
                    target_kind,
                    name,
                    now,
                    now,
                ),
            ).fetchone()
        return str(row["id"])

    def remove_slack_destination(
        self, integration_id: str, destination_id: str
    ) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """DELETE FROM slack_destinations
                   WHERE id=%s AND integration_id=%s""",
                (destination_id, integration_id),
            )
        return result.rowcount == 1

    def set_slack_notification_defaults(
        self, integration_id: str, timer_events: bool, todo_events: bool
    ) -> None:
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE slack_notification_defaults defaults
                   SET timer_events=%s,todo_events=%s,updated_at=%s
                   FROM integrations i
                   WHERE defaults.integration_id=%s AND i.id=defaults.integration_id
                     AND i.provider='slack' AND i.enabled=TRUE""",
                (timer_events, todo_events, utc_now(), integration_id),
            )
            if result.rowcount != 1:
                raise ValueError("Slack integration not found")

    def set_slack_user_notification_rule(
        self,
        integration_id: str,
        user_id: str,
        timer_events: bool | None,
        todo_events: bool | None,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            valid = connection.execute(
                """SELECT 1 FROM integrations i,users u
                   WHERE i.id=%s AND i.provider='slack' AND i.enabled=TRUE
                     AND u.id=%s AND u.enabled=TRUE""",
                (integration_id, user_id),
            ).fetchone()
            if not valid:
                raise ValueError("Slack integration member not found")
            if timer_events is None and todo_events is None:
                connection.execute(
                    """DELETE FROM slack_notification_users
                       WHERE integration_id=%s AND user_id=%s""",
                    (integration_id, user_id),
                )
                return
            connection.execute(
                """INSERT INTO slack_notification_users(
                       integration_id,user_id,timer_events,todo_events,updated_at
                   ) VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT(integration_id,user_id) DO UPDATE SET
                       timer_events=EXCLUDED.timer_events,
                       todo_events=EXCLUDED.todo_events,
                       updated_at=EXCLUDED.updated_at""",
                (integration_id, user_id, timer_events, todo_events, now),
            )

    def slack_notification_users(self, integration_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT u.id,u.email,u.full_name,rule.timer_events,rule.todo_events
                   FROM users u
                   LEFT JOIN slack_notification_users rule
                     ON rule.user_id=u.id AND rule.integration_id=%s
                   WHERE u.enabled=TRUE
                   ORDER BY lower(COALESCE(NULLIF(u.full_name,''),u.email)),u.id""",
                (integration_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_due_slack_messages(
        self,
        observed_at: datetime,
        claim_token: str,
        *,
        limit: int = 25,
        lease_seconds: int = 180,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100 or not 30 <= lease_seconds <= 600:
            raise ValueError("Invalid Slack delivery claim limit")
        with self.connect() as connection:
            rows = connection.execute(
                """WITH ranked AS (
                       SELECT message.id,message.destination_id,
                              ROW_NUMBER() OVER (
                                PARTITION BY message.destination_id
                                ORDER BY message.next_attempt_at,message.created_at,
                                         message.id
                              ) destination_order
                       FROM slack_outbox message
                       JOIN integrations i ON i.id=message.integration_id
                       JOIN slack_destinations destination
                         ON destination.id=message.destination_id
                         WHERE i.provider='slack' AND i.enabled=TRUE
                         AND destination.enabled=TRUE AND message.sent_at IS NULL
                         AND message.discarded_at IS NULL
                         AND message.next_attempt_at<=%s
                         AND (message.claim_until IS NULL OR message.claim_until<=%s)
                         AND NOT EXISTS(
                           SELECT 1 FROM slack_outbox active
                           WHERE active.destination_id=message.destination_id
                             AND active.sent_at IS NULL
                             AND active.discarded_at IS NULL
                             AND active.claim_until>%s
                         )
                   ), due AS (
                       SELECT message.id FROM slack_outbox message
                       JOIN ranked ON ranked.id=message.id
                       WHERE ranked.destination_order=1
                       ORDER BY message.next_attempt_at,message.created_at,message.id
                       FOR UPDATE OF message SKIP LOCKED LIMIT %s
                   ), claimed AS (
                       UPDATE slack_outbox message
                       SET claim_token=%s,
                           claim_until=%s+%s*INTERVAL '1 second',updated_at=%s
                       FROM due WHERE message.id=due.id RETURNING message.*
                   )
                   SELECT claimed.*,destination.slack_target_id,
                          destination.target_kind,destination.display_name,
                          COALESCE(NULLIF(u.full_name,''),u.email,'A team member')
                            user_name,
                          COALESCE(p.name,'an unavailable project') project_name,
                          t.name task_name,g.name todo_name,
                          COALESCE((SELECT SUM(GREATEST(0,EXTRACT(EPOCH FROM
                              (COALESCE(segment.ended_at,claimed.occurred_at)
                               - segment.started_at))))::BIGINT
                            FROM work_session_segments segment
                            WHERE segment.session_id=claimed.session_id),0)
                            tracked_seconds
                   FROM claimed
                   JOIN slack_destinations destination
                     ON destination.id=claimed.destination_id
                   LEFT JOIN users u ON u.id=claimed.user_id
                   LEFT JOIN projects p ON p.id=claimed.project_id
                   LEFT JOIN tasks t ON t.id=claimed.task_id
                   LEFT JOIN global_todos g ON g.id=claimed.todo_id
                   ORDER BY claimed.created_at,claimed.id""",
                (
                    observed_at,
                    observed_at,
                    observed_at,
                    limit,
                    claim_token,
                    observed_at,
                    lease_seconds,
                    observed_at,
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def renew_slack_message_claim(
        self,
        message_id: str,
        claim_token: str,
        observed_at: datetime,
        *,
        lease_seconds: int = 180,
    ) -> bool:
        if not 30 <= lease_seconds <= 600:
            raise ValueError("Invalid Slack delivery claim lease")
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE slack_outbox
                   SET claim_until=%s+%s*INTERVAL '1 second',updated_at=%s
                   WHERE id=%s AND claim_token=%s AND sent_at IS NULL
                     AND discarded_at IS NULL""",
                (observed_at, lease_seconds, observed_at, message_id, claim_token),
            )
        return result.rowcount == 1

    def mark_slack_message_succeeded(
        self,
        message_id: str,
        claim_token: str,
        provider_ts: str,
        observed_at: datetime,
    ) -> bool:
        if not re.fullmatch(r"[0-9]{1,20}\.[0-9]{1,20}", provider_ts):
            raise ValueError("Slack returned an invalid message timestamp")
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE slack_outbox SET provider_ts=%s,sent_at=%s,
                       claim_token=NULL,claim_until=NULL,last_error_code='',
                       updated_at=%s
                   WHERE id=%s AND claim_token=%s AND sent_at IS NULL
                     AND discarded_at IS NULL""",
                (provider_ts, observed_at, observed_at, message_id, claim_token),
            )
        return result.rowcount == 1

    def mark_slack_message_failed(
        self,
        message_id: str,
        claim_token: str,
        observed_at: datetime,
        error_code: str,
        retry_seconds: int,
    ) -> bool:
        safe_code = re.sub(r"[^a-z0-9_]", "_", error_code.lower())[:64]
        retry_seconds = min(max(int(retry_seconds), 1), 21_600)
        discard_after_attempts = 25
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE slack_outbox
                   SET attempt_count=attempt_count+1,
                       next_attempt_at=%s+%s*INTERVAL '1 second',
                       claim_token=NULL,claim_until=NULL,last_error_code=%s,
                       discarded_at=CASE
                         WHEN attempt_count+1 >= %s THEN %s
                         ELSE discarded_at END,
                       updated_at=%s
                   WHERE id=%s AND claim_token=%s AND sent_at IS NULL
                     AND discarded_at IS NULL""",
                (
                    observed_at,
                    retry_seconds,
                    safe_code,
                    discard_after_attempts,
                    observed_at,
                    observed_at,
                    message_id,
                    claim_token,
                ),
            )
        return result.rowcount == 1

    def disconnect_slack_integration(self, integration_id: str) -> None:
        now = utc_now()
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE integrations SET enabled=FALSE,updated_at=%s,
                          sync_claim_token=NULL,sync_claim_until=NULL
                   WHERE id=%s AND provider='slack'""",
                (now, integration_id),
            )
            if result.rowcount != 1:
                raise ValueError("Slack integration not found")
            connection.execute(
                "DELETE FROM integration_credentials WHERE integration_id=%s",
                (integration_id,),
            )
            connection.execute(
                "DELETE FROM slack_outbox WHERE integration_id=%s AND sent_at IS NULL",
                (integration_id,),
            )

    def delete_unconfigured_slack_integration(self, integration_id: str) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """DELETE FROM integrations i
                   WHERE i.id=%s AND i.provider='slack'
                     AND NOT EXISTS(SELECT 1 FROM integration_credentials c
                                    WHERE c.integration_id=i.id)
                     AND NOT EXISTS(SELECT 1 FROM slack_destinations d
                                    WHERE d.integration_id=i.id)
                     AND NOT EXISTS(SELECT 1 FROM slack_outbox message
                                    WHERE message.integration_id=i.id)""",
                (integration_id,),
            )
        return result.rowcount == 1

    def purge_slack_outbox(self, cutoff: datetime, *, limit: int = 1000) -> int:
        if not 1 <= limit <= 10_000:
            raise ValueError("Invalid Slack outbox cleanup limit")
        with self.connect() as connection:
            result = connection.execute(
                """DELETE FROM slack_outbox WHERE id IN (
                       SELECT id FROM slack_outbox
                       WHERE COALESCE(sent_at,discarded_at)<%s
                         AND (sent_at IS NOT NULL OR discarded_at IS NOT NULL)
                       ORDER BY COALESCE(sent_at,discarded_at) LIMIT %s
                   )""",
                (cutoff, limit),
            )
        return result.rowcount

from __future__ import annotations

from .base import RepositoryMixin


class RateLimitsRepository(RepositoryMixin):
    def consume_request_limit(
        self, bucket_key: str, *, limit: int, window_seconds: int
    ) -> tuple[bool, int]:
        """Atomically consume a shared PostgreSQL fixed-window request bucket."""
        with self.connect() as connection:
            row = connection.execute(
                """WITH timing AS (
                       SELECT floor(
                           extract(epoch FROM statement_timestamp()) / %s
                       )::bigint AS window_id
                   ), consumed AS (
                       INSERT INTO request_rate_limits(
                           bucket_key,window_id,request_count,updated_at
                       )
                       SELECT %s,window_id,1,CURRENT_TIMESTAMP FROM timing
                       ON CONFLICT(bucket_key) DO UPDATE SET
                           window_id=EXCLUDED.window_id,
                           request_count=CASE
                               WHEN request_rate_limits.window_id=EXCLUDED.window_id
                               THEN request_rate_limits.request_count + 1
                               ELSE 1
                           END,
                           updated_at=CURRENT_TIMESTAMP
                       RETURNING window_id,request_count
                   )
                   SELECT request_count,
                          greatest(1,ceil(
                              ((window_id + 1) * %s) -
                              extract(epoch FROM statement_timestamp())
                          ))::integer retry_after
                     FROM consumed""",
                (window_seconds, bucket_key, window_seconds),
            ).fetchone()
        return int(row["request_count"]) <= limit, int(row["retry_after"])

    def purge_request_limits_before(self, cutoff) -> int:
        with self.connect() as connection:
            result = connection.execute(
                "DELETE FROM request_rate_limits WHERE updated_at < %s", (cutoff,)
            )
        return result.rowcount

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TypeVar

from ..database import Database
from ..observability import MetricsRegistry

LOGGER = logging.getLogger("dayfinch-background-jobs")
Result = TypeVar("Result")


class BackgroundJobCoordinator:
    """Database-backed leases keep periodic jobs singleton across API replicas."""

    def __init__(
        self,
        database: Database,
        owner_id: str,
        metrics: MetricsRegistry | None = None,
    ):
        self.database = database
        self.owner_id = owner_id
        self.metrics = metrics

    def run(
        self, name: str, lease_seconds: int, function: Callable[[], Result]
    ) -> Result | None:
        started = time.perf_counter()
        if not self.database.claim_background_job(name, self.owner_id, lease_seconds):
            if self.metrics:
                self.metrics.increment(
                    "dayfinch_background_jobs_total", job=name, outcome="lease_skipped"
                )
            return None
        try:
            result = function()
        except Exception:
            self._observe(name, "failed", started)
            raise
        self._observe(name, "succeeded", started)
        return result

    def _observe(self, name: str, outcome: str, started: float) -> None:
        duration = time.perf_counter() - started
        if self.metrics:
            labels = {"job": name, "outcome": outcome}
            self.metrics.increment("dayfinch_background_jobs_total", **labels)
            self.metrics.increment(
                "dayfinch_background_job_duration_seconds_sum", duration, **labels
            )
            self.metrics.increment(
                "dayfinch_background_job_duration_seconds_count", **labels
            )
        LOGGER.info(
            "background_job_completed",
            extra={
                "job": name,
                "outcome": outcome,
                "duration_ms": round(duration * 1000, 3),
            },
        )

    def release(self) -> int:
        return self.database.release_background_jobs(self.owner_id)


async def run_periodic_job(
    coordinator: BackgroundJobCoordinator,
    *,
    name: str,
    interval_seconds: int,
    lease_seconds: int,
    function: Callable[[], object],
    run_immediately: bool = True,
) -> None:
    if not run_immediately:
        await asyncio.sleep(interval_seconds)
    while True:
        try:
            # Cancelling asyncio.to_thread() abandons only the awaiter; the worker
            # thread keeps using the database. Shield it and join it on shutdown so
            # a closing replica cannot release leases or close its pool underneath
            # an in-flight job.
            worker = asyncio.create_task(
                asyncio.to_thread(coordinator.run, name, lease_seconds, function)
            )
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                await worker
                raise
        except Exception as exc:  # noqa: BLE001 - periodic workers must survive
            LOGGER.error(
                "background_job_retry_scheduled",
                extra={"job": name, "exception_type": type(exc).__name__},
            )
        await asyncio.sleep(interval_seconds)

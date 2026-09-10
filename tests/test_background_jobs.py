import asyncio
import contextlib
import threading

from api.services.background_jobs import BackgroundJobCoordinator, run_periodic_job


def test_only_one_replica_can_run_a_leased_job(database):
    first = BackgroundJobCoordinator(database, "replica-a")
    second = BackgroundJobCoordinator(database, "replica-b")
    calls: list[str] = []

    assert first.run("retention", 3_600, lambda: calls.append("first")) is None
    assert second.run("retention", 3_600, lambda: calls.append("second")) is None
    assert calls == ["first"]

    assert first.release() == 1
    assert second.run("retention", 3_600, lambda: calls.append("second")) is None
    assert calls == ["first", "second"]


def test_same_replica_can_renew_a_job_lease(database):
    coordinator = BackgroundJobCoordinator(database, "replica-a")
    calls: list[int] = []

    coordinator.run("scheduled-reports", 3_600, lambda: calls.append(1))
    coordinator.run("scheduled-reports", 3_600, lambda: calls.append(2))

    assert calls == [1, 2]


def test_periodic_job_shutdown_joins_the_in_flight_worker():
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class Coordinator:
        @staticmethod
        def run(_name, _lease_seconds, function):
            return function()

    def blocked_job():
        entered.set()
        release.wait(timeout=5)
        finished.set()

    async def scenario():
        task = asyncio.create_task(
            run_periodic_job(
                Coordinator(),
                name="test-job",
                interval_seconds=3600,
                lease_seconds=3600,
                function=blocked_job,
            )
        )
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()
        assert not finished.is_set()
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert finished.is_set()

    asyncio.run(scenario())

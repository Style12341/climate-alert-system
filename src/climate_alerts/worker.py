import asyncio
import signal
import typing
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from functools import partial

import structlog
from apscheduler.schedulers.asyncio import (  # type: ignore[import-untyped]
    AsyncIOScheduler,
)

from climate_alerts.config import Settings, get_settings
from climate_alerts.db import SessionFactory, build_engine, build_session_factory
from climate_alerts.logs import configure_logging
from climate_alerts.notifications.dispatch import dispatch_due
from climate_alerts.notifications.evaluation import evaluate_alerts
from climate_alerts.notifications.senders import (
    NotificationSender,
    build_sender_registry,
)

# How long cancelled jobs get to unwind. An internal bound that keeps `run()`
# from hanging on a job stuck in cleanup, not a value an operator would tune.
_CANCELLATION_GRACE_SECONDS = 0.5

logger = structlog.get_logger()


class Worker:
    """Runs evaluation and dispatch on intervals until asked to stop.

    The caller owns the engine behind `sessions` and disposes it after `run()`
    returns.
    """

    def __init__(
        self,
        settings: Settings,
        sessions: SessionFactory,
        senders: Mapping[str, NotificationSender],
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._senders = senders
        self._stopping = asyncio.Event()
        self._running: set[asyncio.Task[object]] = set()

    def request_stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        """Run the jobs until `request_stop`, then drain them."""
        scheduler = AsyncIOScheduler(timezone=UTC)
        self._add_job(
            scheduler,
            "evaluation",
            partial(evaluate_alerts, self._sessions, self._settings, self._stopping),
            self._settings.evaluation_interval_seconds,
        )
        self._add_job(
            scheduler,
            "dispatch",
            partial(
                dispatch_due,
                self._sessions,
                self._senders,
                self._settings,
                self._stopping,
            ),
            self._settings.dispatch_interval_seconds,
        )
        scheduler.start()

        await self._stopping.wait()

        scheduler.pause()
        logger.info("worker.stopping")
        # APScheduler cancels running jobs on shutdown and cannot wait for
        # them, even with wait=True, so they are drained here first.
        await self._wait_for_running(self._settings.worker_shutdown_timeout_seconds)
        drained = not self._running

        scheduler.shutdown(wait=False)
        # AsyncIOScheduler only queues the shutdown on the loop, so nothing is
        # cancelled until this coroutine yields.
        await asyncio.sleep(0)
        await self._wait_for_running(_CANCELLATION_GRACE_SECONDS)
        logger.info("worker.stopped", drained=drained)

    def _add_job(
        self,
        scheduler: AsyncIOScheduler,
        name: str,
        job: Callable[[], Awaitable[object]],
        interval_seconds: float,
    ) -> None:
        scheduler.add_job(
            self._run_contained,
            args=(name, job),
            trigger="interval",
            seconds=interval_seconds,
            id=name,
            name=name,
            max_instances=1,
            coalesce=True,
            next_run_time=datetime.now(UTC),
            # The default of 1 s silently drops a run when the loop stalls.
            misfire_grace_time=None,
        )

    async def _run_contained(
        self, name: str, job: Callable[[], Awaitable[object]]
    ) -> None:
        """Run one job, keeping any failure from reaching the scheduler.

        Catches `Exception` only: cancellation has to reach the job. The next
        interval is the retry.
        """
        # A job can be queued just before the stop and first run just after.
        if self._stopping.is_set():
            return

        # Never None: this always runs as a task on the loop.
        task = typing.cast(asyncio.Task[object], asyncio.current_task())
        self._running.add(task)
        task.add_done_callback(self._running.discard)
        try:
            await job()
        except Exception as exc:
            # The type only: driver messages can carry connection details.
            logger.error("worker.job_failed", job=name, error_type=type(exc).__name__)

    async def _wait_for_running(self, limit_seconds: float) -> None:
        if self._running:
            await asyncio.wait(self._running, timeout=limit_seconds)


async def _serve(settings: Settings, senders: Mapping[str, NotificationSender]) -> None:
    engine = build_engine(settings)
    worker = Worker(settings, build_session_factory(engine), senders)
    loop = asyncio.get_running_loop()
    for stop_signal in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(stop_signal, worker.request_stop)
    try:
        await worker.run()
    finally:
        await engine.dispose()


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    # Left uncaught on purpose: an unknown channel must stop the process with
    # a non-zero exit before the scheduler starts.
    senders = build_sender_registry(settings.enabled_channels)
    asyncio.run(_serve(settings, senders))


if __name__ == "__main__":
    main()

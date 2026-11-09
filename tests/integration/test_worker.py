import asyncio
import json
import os
import signal
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError
from structlog.testing import capture_logs

from climate_alerts.config import Settings
from climate_alerts.db import SessionFactory
from climate_alerts.notifications.evaluation import EvaluationResult
from climate_alerts.notifications.senders import build_sender_registry
from climate_alerts.worker import Worker

# Long enough that a job running at all proves the immediate first run.
NEVER_AGAIN = {"evaluation_interval_seconds": 3600, "dispatch_interval_seconds": 3600}
EMPTY_EVALUATION = EvaluationResult(created=0, chunks=0, failed_chunks=0)

StartWorker = Callable[..., tuple[Worker, asyncio.Task[None]]]


@pytest.fixture
async def start_worker(
    settings: Settings, sessions: SessionFactory
) -> AsyncIterator[StartWorker]:
    """Start workers on the test's loop and stop any that a test left running."""
    started: list[tuple[Worker, asyncio.Task[None]]] = []

    def start(**overrides: float) -> tuple[Worker, asyncio.Task[None]]:
        worker = Worker(
            settings.model_copy(update={**NEVER_AGAIN, **overrides}),
            sessions,
            build_sender_registry(("whatsapp",)),
        )
        task = asyncio.create_task(worker.run())
        started.append((worker, task))
        return worker, task

    yield start

    for worker, _ in started:
        worker.request_stop()
    await asyncio.wait_for(
        asyncio.gather(*(task for _, task in started), return_exceptions=True), 30
    )


async def wait_until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(10):
        while not condition():  # noqa: ASYNC110 - watches captured logs and counters
            await asyncio.sleep(0.01)


def events(logs: list[Any]) -> list[str]:
    return [entry["event"] for entry in logs]


async def test_jobs_run_immediately_on_start(start_worker: StartWorker) -> None:
    with capture_logs() as logs:
        start_worker()
        await wait_until(
            lambda: {"evaluation.completed", "dispatch.completed"} <= set(events(logs))
        )


@pytest.mark.parametrize(
    "error",
    [
        OperationalError("SELECT 1", {}, Exception("host=db.internal")),
        OSError("host=db.internal"),
    ],
    ids=["sqlalchemy", "raw-driver"],
)
async def test_job_failure_does_not_stop_the_worker(
    start_worker: StartWorker, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    calls = 0

    async def fail_once(*_: object) -> EvaluationResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error
        return EMPTY_EVALUATION

    monkeypatch.setattr("climate_alerts.worker.evaluate_alerts", fail_once)

    with capture_logs() as logs:
        _, task = start_worker(evaluation_interval_seconds=0.05)
        await wait_until(lambda: calls >= 2)

    assert not task.done()
    failures = [entry for entry in logs if entry["event"] == "worker.job_failed"]
    assert len(failures) == 1
    assert failures[0]["job"] == "evaluation"
    assert failures[0]["error_type"] == type(error).__name__
    assert "db.internal" not in str(logs)


async def test_stop_waits_for_in_flight_job(
    start_worker: StartWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()
    finished = False

    async def slow_chunk(
        _sessions: object, _settings: object, stopping: asyncio.Event
    ) -> EvaluationResult:
        nonlocal finished
        started.set()
        await stopping.wait()
        await asyncio.sleep(0.1)  # the rest of the chunk, after the stop request
        finished = True
        return EMPTY_EVALUATION

    monkeypatch.setattr("climate_alerts.worker.evaluate_alerts", slow_chunk)

    with capture_logs() as logs:
        worker, task = start_worker()
        await asyncio.wait_for(started.wait(), 10)
        worker.request_stop()
        await asyncio.wait_for(task, 10)

    assert finished
    stopped = [entry for entry in logs if entry["event"] == "worker.stopped"]
    assert [entry["drained"] for entry in stopped] == [True]


async def test_stop_gives_up_after_timeout(
    start_worker: StartWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()
    cancelled = False

    async def ignores_stopping(*_: object) -> EvaluationResult:
        nonlocal cancelled
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled = True
            raise
        return EMPTY_EVALUATION

    monkeypatch.setattr("climate_alerts.worker.evaluate_alerts", ignores_stopping)
    timeout = 0.3

    with capture_logs() as logs:
        worker, task = start_worker(worker_shutdown_timeout_seconds=timeout)
        await asyncio.wait_for(started.wait(), 10)
        loop = asyncio.get_running_loop()
        stop_requested = loop.time()
        worker.request_stop()
        await asyncio.wait_for(task, 10)
        elapsed = loop.time() - stop_requested

    assert timeout <= elapsed < timeout + 1
    assert cancelled
    stopped = [entry for entry in logs if entry["event"] == "worker.stopped"]
    assert [entry["drained"] for entry in stopped] == [False]


async def test_stop_before_run_never_starts_a_job(
    settings: Settings,
    sessions: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def count_call(*_: object) -> EvaluationResult:
        nonlocal calls
        calls += 1
        return EMPTY_EVALUATION

    monkeypatch.setattr("climate_alerts.worker.evaluate_alerts", count_call)
    worker = Worker(
        settings.model_copy(update=NEVER_AGAIN),
        sessions,
        build_sender_registry(("whatsapp",)),
    )

    worker.request_stop()
    await asyncio.wait_for(worker.run(), 10)

    assert calls == 0


async def test_job_queued_before_the_stop_does_nothing_after_it(
    settings: Settings, sessions: SessionFactory
) -> None:
    """Exercises the guard directly: `run()` pauses before a queued job can start."""
    calls = 0

    async def job() -> None:
        nonlocal calls
        calls += 1

    worker = Worker(settings, sessions, build_sender_registry(("whatsapp",)))
    worker.request_stop()

    await worker._run_contained("evaluation", job)

    assert calls == 0


def worker_environment(database_url: str, channels: str) -> dict[str, str]:
    return {
        **os.environ,
        "DATABASE_URL": database_url,
        "JWT_SECRET": "x" * 32,
        "NOTIFICATION_CHANNELS": channels,
        "LOG_LEVEL": "INFO",
    }


async def test_sigterm_stops_subprocess_cleanly(
    postgres_url: str, tmp_path: Path
) -> None:
    # In a temporary directory so a developer's .env cannot change the run.
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "climate_alerts.worker",
        cwd=tmp_path,
        env=worker_environment(postgres_url, "whatsapp"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert child.stdout is not None
    output = bytearray()
    try:
        # A completed job means the signal handlers are installed and the
        # child reached the database, so no fixed sleep is needed.
        async with asyncio.timeout(60):
            while b"evaluation.completed" not in output:
                line = await child.stdout.readline()
                assert line, f"worker exited early: {output.decode()}"
                output += line

            child.send_signal(signal.SIGTERM)
            rest, _ = await child.communicate()
            output += rest
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()

    assert child.returncode == 0, output.decode()
    logged = [
        json.loads(line)
        for line in output.decode().splitlines()
        if line.startswith("{")
    ]
    assert "worker.stopped" in [entry["event"] for entry in logged]


async def test_unknown_channel_refuses_to_start(
    postgres_url: str, tmp_path: Path
) -> None:
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "climate_alerts.worker",
        cwd=tmp_path,
        env=worker_environment(postgres_url, "carrier-pigeon"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        async with asyncio.timeout(60):
            output, _ = await child.communicate()
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()

    assert child.returncode != 0
    assert b"carrier-pigeon" in output
    assert b"evaluation.completed" not in output

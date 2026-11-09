#!/usr/bin/env python3
"""Measure the real evaluation and claim statements at the expected volume.

Run: BENCHMARK_DATABASE_URL=postgresql+asyncpg://user:pass@127.0.0.1:PORT/db \\
     uv run python scripts/volume_benchmark.py

DESTRUCTIVE: it migrates the target database and TRUNCATES every table except
the event catalog. It refuses any host but localhost or 127.0.0.1, and it reads
its own variable, not DATABASE_URL, so it cannot pick up the application's
database by accident. Point it at a throwaway `postgres:17` container.

It loads 24,000 farms, 16 days, 7 events and 3 alerts per farm through the
schema of the migration, with every forecast over its threshold. It then runs
the worst case in chunks of 5,000 alerts with one channel, and drains the
resulting 1,152,000 notifications in batches of 500. Each step prints wall
time, WAL and peak client memory.

What it does NOT do:

* It is not a load test of the running system. There is no API traffic and no
  second evaluator or dispatcher running at the same time.
* It does not send anything or simulate a provider's latency, so the drain
  measures the database and the client, not a delivery rate.
* It does not represent production hardware: one machine, default PostgreSQL
  settings, and whatever else that machine is doing.
* It does not measure sustained queue behaviour. One drain says little about
  autovacuum, so the script reports whether autovacuum ran during it.

Traps, because getting these wrong is quiet:

* The empty `notifications` table must be ANALYZEd before the run. That is the
  state of a fresh deployment and the one where an anti-join plan collapsed
  (decision 12), so the script checks `reltuples` instead of assuming it.
* WAL deltas include anything else writing at that moment, such as autovacuum
  or full-page images after a checkpoint. Read them as upper bounds.
* Peak memory is VmHWM from /proc/self/status. `ru_maxrss` survives exec and
  can report the peak of the `uv` parent. VmHWM never goes down, so the drain's
  figure is the peak of the whole process so far.
* Alembic's env.py calls asyncio.run, so migrating has to happen before this
  script starts its own event loop.
"""

import asyncio
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from climate_alerts.db import SessionFactory, build_session_factory
from climate_alerts.notifications.dispatch import (
    Outcome,
    build_claim_statement,
    record_outcomes,
)
from climate_alerts.notifications.evaluation import (
    build_evaluation_statement,
    next_chunk_end,
)

ROOT = Path(__file__).resolve().parent.parent
ALLOWED_HOSTS = {"localhost", "127.0.0.1"}

FARMS = 24_000
USERS = 10_000
DAYS = 16
EVENTS = 7
ALERTS_PER_FARM = 3
CHUNK_SIZE = 5_000
BATCH_SIZE = 500
LEASE_SECONDS = 300
CHANNELS = ["whatsapp"]

EXPECTED_FORECASTS = FARMS * DAYS * EVENTS
EXPECTED_ALERTS = FARMS * ALERTS_PER_FARM
EXPECTED_NOTIFICATIONS = EXPECTED_ALERTS * DAYS * len(CHANNELS)

MIB = 1024 * 1024

TRUNCATE = """
TRUNCATE notifications, alerts, weather_forecasts, fields, users
RESTART IDENTITY CASCADE
"""
LOAD_USERS = """
INSERT INTO users (id, phone) OVERRIDING SYSTEM VALUE
SELECT g, '+54911' || lpad(g::text, 7, '0') FROM generate_series(1, $1::int) g
"""
LOAD_FIELDS = """
INSERT INTO fields (id, owner_id, name, timezone) OVERRIDING SYSTEM VALUE
SELECT g, ((g - 1) % $2::int) + 1, 'field-' || g,
       (ARRAY['America/Argentina/Buenos_Aires', 'America/Montevideo',
              'America/Asuncion', 'America/Bogota', 'America/Mexico_City',
              'America/Sao_Paulo'])[(g - 1) % 6 + 1]
FROM generate_series(1, $1::int) g
"""
# Probability 90 against threshold 70 makes every alert fire on every date.
# The dates start on the UTC date, which is never earlier than the local date
# in these timezones, so all of them still count as "today or later".
LOAD_FORECASTS = """
INSERT INTO weather_forecasts
    (field_id, forecast_date, event_type, probability, fetched_at)
SELECT f.id, (now() AT TIME ZONE 'UTC')::date + d, e.code, 90, now()
FROM fields f
CROSS JOIN generate_series(0, $1::int - 1) d
CROSS JOIN climate_event_types e
ORDER BY 1, 2, 3
"""
LOAD_ALERTS = """
INSERT INTO alerts (id, field_id, event_type, threshold) OVERRIDING SYSTEM VALUE
SELECT (f.id - 1) * 3 + k, f.id,
       CASE k
         WHEN 1 THEN 'frost'
         WHEN 2 THEN (ARRAY['rain', 'hail', 'snow', 'thunderstorm', 'heat_wave',
                            'strong_wind'])[(f.id - 1) % 6 + 1]
         ELSE (ARRAY['rain', 'hail', 'snow', 'thunderstorm', 'heat_wave',
                     'strong_wind'])[f.id % 6 + 1]
       END,
       70
FROM fields f CROSS JOIN generate_series(1, 3) k
"""


def checked_database_url() -> URL:
    """Refuse anything but a local database, because this script truncates."""
    raw = os.environ.get("BENCHMARK_DATABASE_URL")
    if not raw:
        raise SystemExit("set BENCHMARK_DATABASE_URL to a throwaway local database")
    url = make_url(raw)
    if url.host not in ALLOWED_HOSTS:
        raise SystemExit(
            f"refusing host {url.host!r}: this script truncates tables, "
            f"so only {sorted(ALLOWED_HOSTS)} are accepted"
        )
    return url


def migrate(url: URL) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    # ConfigParser treats % as interpolation, so a password containing it breaks.
    rendered = url.render_as_string(hide_password=False).replace("%", "%%")
    config.set_main_option("sqlalchemy.url", rendered)
    command.upgrade(config, "head")


def peak_client_mib() -> float:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) / 1024
    raise RuntimeError("VmHWM not found in /proc/self/status")


def require(actual: float, expected: float, what: str) -> None:
    if actual != expected:
        raise RuntimeError(f"{what}: expected {expected}, got {actual}")


async def scalar(monitor: AsyncConnection, sql: str) -> float:
    return (await monitor.exec_driver_sql(sql)).scalar_one()


@asynccontextmanager
async def measured(monitor: AsyncConnection, label: str) -> AsyncIterator[None]:
    lsn_query = "SELECT pg_current_wal_lsn()::text"
    before = await scalar(monitor, lsn_query)
    started = time.perf_counter()
    yield
    wall = time.perf_counter() - started
    after = await scalar(monitor, lsn_query)
    wal = await scalar(monitor, f"SELECT pg_wal_lsn_diff('{after}', '{before}')")
    print(
        f"{label}: wall {wall:.2f} s, WAL {float(wal) / MIB:.1f} MiB, "
        f"peak client {peak_client_mib():.1f} MiB",
        flush=True,
    )


async def load(monitor: AsyncConnection) -> None:
    """Fill the tables and leave `notifications` analyzed while empty."""
    await monitor.exec_driver_sql(TRUNCATE)
    await monitor.exec_driver_sql(LOAD_USERS, (USERS,))
    await monitor.exec_driver_sql(LOAD_FIELDS, (FARMS, USERS))
    await monitor.exec_driver_sql(LOAD_FORECASTS, (DAYS,))
    await monitor.exec_driver_sql(LOAD_ALERTS)
    await monitor.exec_driver_sql("VACUUM ANALYZE")

    forecasts = await scalar(monitor, "SELECT count(*) FROM weather_forecasts")
    alerts = await scalar(monitor, "SELECT count(*) FROM alerts")
    require(forecasts, EXPECTED_FORECASTS, "forecasts loaded")
    require(alerts, EXPECTED_ALERTS, "alerts loaded")
    # 0 after ANALYZE; -1 would mean the table has no statistics yet.
    reltuples = await scalar(
        monitor, "SELECT reltuples FROM pg_class WHERE relname = 'notifications'"
    )
    require(reltuples, 0, "notifications reltuples (analyzed while empty)")


async def evaluate(sessions: SessionFactory) -> None:
    """Run the worst case exactly as `evaluate_alerts` walks its chunks."""
    after_id = 0
    created = 0
    chunk_seconds: list[float] = []
    while True:
        async with sessions() as session:
            up_to_id = await next_chunk_end(session, after_id, CHUNK_SIZE)
        if up_to_id is None:
            break
        statement = build_evaluation_statement(after_id, up_to_id, CHANNELS)
        started = time.perf_counter()
        async with sessions.begin() as session:
            created += (await session.execute(statement)).rowcount
        chunk_seconds.append(time.perf_counter() - started)
        after_id = up_to_id

    require(created, EXPECTED_NOTIFICATIONS, "notifications created")
    print(
        f"evaluation chunks: {len(chunk_seconds)}, "
        f"slowest {max(chunk_seconds):.2f} s, rows {created}",
        flush=True,
    )


async def drain(sessions: SessionFactory) -> None:
    """Claim and record with the real code. No send happens between the two."""
    claimed = 0
    batches = 0
    while True:
        statement = build_claim_statement(BATCH_SIZE, LEASE_SECONDS)
        async with sessions.begin() as session:
            rows = (await session.execute(statement)).all()
        if not rows:
            break
        if len(rows) > BATCH_SIZE:
            raise RuntimeError(f"claim returned {len(rows)} rows")
        await record_outcomes(
            sessions, [Outcome(r.id, r.attempts, "sent", None, None) for r in rows]
        )
        claimed += len(rows)
        batches += 1

    require(claimed, EXPECTED_NOTIFICATIONS, "notifications claimed")
    print(f"drain batches: {batches}, rows {claimed}", flush=True)


async def run(url: URL) -> None:
    engine = create_async_engine(url, pool_size=2, max_overflow=0)
    monitor_engine = engine.execution_options(isolation_level="AUTOCOMMIT")
    sessions = build_session_factory(engine)
    vacuums = (
        "SELECT autovacuum_count FROM pg_stat_user_tables "
        "WHERE relname = 'notifications'"
    )
    try:
        async with monitor_engine.connect() as monitor:
            print(await scalar(monitor, "SELECT version()"), flush=True)
            print(f"logical CPUs: {os.cpu_count()}", flush=True)
            async with measured(monitor, "load"):
                await load(monitor)
            async with measured(monitor, "evaluation worst case"):
                await evaluate(sessions)
            vacuums_before = await scalar(monitor, vacuums)
            async with measured(monitor, "drain"):
                await drain(sessions)
            vacuums_after = await scalar(monitor, vacuums)
            sent = await scalar(
                monitor,
                "SELECT count(*) FROM notifications WHERE status = 'sent'",
            )
            require(sent, EXPECTED_NOTIFICATIONS, "notifications sent")
            print(
                f"autovacuum runs on notifications during the drain: "
                f"{vacuums_after - vacuums_before}",
                flush=True,
            )
    finally:
        await engine.dispose()


def main() -> None:
    url = checked_database_url()
    migrate(url)
    asyncio.run(run(url))


if __name__ == "__main__":
    main()

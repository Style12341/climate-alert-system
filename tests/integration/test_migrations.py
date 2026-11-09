import asyncio
from collections.abc import Iterator

import pytest
from alembic import command
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from climate_alerts.agrobot.models import ClimateEventType
from climate_alerts.db import SessionFactory
from tests.conftest import alembic_config

SCRATCH_DATABASE = "migration_cycle"
ALL_TABLES = {
    "users",
    "fields",
    "climate_event_types",
    "weather_forecasts",
    "alerts",
    "notifications",
}


async def _run_autocommit(url: str, statement: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


async def _application_tables(url: str) -> set[str]:
    """Table names, minus the bookkeeping table Alembic keeps after `downgrade base`."""
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            names = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_table_names()
            )
    finally:
        await engine.dispose()
    return set(names) - {"alembic_version"}


@pytest.fixture
def scratch_url(postgres_url: str) -> Iterator[str]:
    """A separate empty database, so the cycle cannot disturb the shared schema."""
    asyncio.run(_run_autocommit(postgres_url, f"CREATE DATABASE {SCRATCH_DATABASE}"))
    yield (
        make_url(postgres_url)
        .set(database=SCRATCH_DATABASE)
        .render_as_string(hide_password=False)
    )
    asyncio.run(_run_autocommit(postgres_url, f"DROP DATABASE {SCRATCH_DATABASE}"))


# Sync on purpose: Alembic's env.py calls asyncio.run.
def test_upgrade_check_downgrade_cycle(scratch_url: str) -> None:
    config = alembic_config(scratch_url)

    command.upgrade(config, "head")
    assert asyncio.run(_application_tables(scratch_url)) == ALL_TABLES
    command.check(config)

    command.downgrade(config, "base")
    assert asyncio.run(_application_tables(scratch_url)) == set()

    command.upgrade(config, "head")
    assert asyncio.run(_application_tables(scratch_url)) == ALL_TABLES


async def test_migration_seeds_catalog(sessions: SessionFactory) -> None:
    async with sessions() as session:
        rows = await session.execute(
            select(ClimateEventType.code, ClimateEventType.label)
        )

    assert {code: label for code, label in rows} == {
        "frost": "Helada",
        "rain": "Lluvia",
        "hail": "Granizo",
        "snow": "Nieve",
        "thunderstorm": "Tormenta",
        "heat_wave": "Ola de calor",
        "strong_wind": "Viento fuerte",
    }

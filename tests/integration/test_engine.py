import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from climate_alerts.config import Settings
from climate_alerts.db import build_engine


@pytest.fixture
async def one_connection_engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = build_engine(
        settings.model_copy(update={"db_pool_size": 1, "db_max_overflow": 0})
    )
    yield engine
    await engine.dispose()


async def test_pool_replaces_a_connection_postgres_dropped(
    settings: Settings, one_connection_engine: AsyncEngine
) -> None:
    async with one_connection_engine.connect() as connection:
        pid = await connection.scalar(text("SELECT pg_backend_pid()"))

    admin = create_async_engine(
        settings.database_url.get_secret_value(), poolclass=NullPool
    )
    try:
        async with admin.connect() as connection:
            await connection.execute(
                text("SELECT pg_terminate_backend(:pid)"), {"pid": pid}
            )
    finally:
        await admin.dispose()

    async with one_connection_engine.connect() as connection:
        assert await connection.scalar(text("SELECT 1")) == 1


async def test_query_on_a_stalled_connection_gives_up(settings: Settings) -> None:
    engine = build_engine(
        settings.model_copy(update={"db_command_timeout_seconds": 0.2})
    )
    try:
        async with asyncio.timeout(4), engine.connect() as connection:
            with pytest.raises(TimeoutError):
                await connection.execute(text("SELECT pg_sleep(30)"))
    finally:
        await engine.dispose()

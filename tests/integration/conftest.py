from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from climate_alerts.agrobot.models import ClimateEventType
from climate_alerts.config import Settings
from climate_alerts.db import (
    Base,
    SessionFactory,
    build_engine,
    build_session_factory,
)

# Registers every table on Base.metadata; clean_tables relies on it.
from climate_alerts.notifications import models as _notification_models  # noqa: F401


@pytest.fixture(scope="session")
async def sessions(settings: Settings) -> AsyncIterator[SessionFactory]:
    engine = build_engine(settings)
    yield build_session_factory(engine)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def clean_tables(sessions: SessionFactory) -> None:
    """Empty every table but the catalog, which the migration seeds.

    Truncates instead of rolling back because concurrency tests need real commits.
    """
    tables = ", ".join(
        table.name
        for table in Base.metadata.sorted_tables
        if table is not ClimateEventType.__table__
    )
    async with sessions() as session:
        await session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        await session.commit()

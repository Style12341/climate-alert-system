import asyncio
import time
import typing
from collections.abc import Sequence
from dataclasses import dataclass

import structlog
from sqlalchemy import CursorResult, Date, Text, bindparam, cast, func, select, true
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from climate_alerts.agrobot.models import Field, WeatherForecast
from climate_alerts.alerts.models import Alert
from climate_alerts.config import Settings
from climate_alerts.db import SessionFactory
from climate_alerts.notifications.models import Notification


@dataclass(frozen=True)
class EvaluationResult:
    """`chunks` counts every chunk attempted, including the failed ones."""

    created: int
    chunks: int
    failed_chunks: int


def build_evaluation_statement(
    after_id: int, up_to_id: int, channels: Sequence[str]
) -> postgresql.Insert:
    """Insert a notification per channel for each date over its threshold.

    Covers the live alerts in `(after_id, up_to_id]`. The ORDER BY makes
    concurrent evaluators take conflicting rows in the same order, so they
    wait on each other instead of deadlocking.
    """
    channel = (
        func.unnest(bindparam("channels", list(channels), type_=postgresql.ARRAY(Text)))
        .table_valued("channel")
        .render_derived(name="c")
    )
    local_today = cast(func.timezone(Field.timezone, func.now()), Date)
    candidates = (
        select(
            Alert.id,
            WeatherForecast.forecast_date,
            channel.c.channel,
            WeatherForecast.probability,
            Alert.threshold,
        )
        .join(Field, Field.id == Alert.field_id)
        .join(
            WeatherForecast,
            (WeatherForecast.field_id == Alert.field_id)
            & (WeatherForecast.event_type == Alert.event_type),
        )
        .join(channel, true())
        .where(
            Alert.deleted_at.is_(None),
            Alert.id > after_id,
            Alert.id <= up_to_id,
            WeatherForecast.forecast_date >= local_today,
            WeatherForecast.probability >= Alert.threshold,
        )
        .order_by(Alert.id, WeatherForecast.forecast_date, channel.c.channel)
    )
    return (
        postgresql.insert(Notification)
        .from_select(
            ["alert_id", "forecast_date", "channel", "probability", "threshold"],
            candidates,
        )
        .on_conflict_do_nothing(constraint="uq_notifications_alert_date_channel")
    )


async def next_chunk_end(
    session: AsyncSession, after_id: int, chunk_size: int
) -> int | None:
    """Return the highest id of the next `chunk_size` live alerts, if any."""
    chunk = (
        select(Alert.id)
        .where(Alert.deleted_at.is_(None), Alert.id > after_id)
        .order_by(Alert.id)
        .limit(chunk_size)
        .subquery()
    )
    highest_id: int | None = await session.scalar(select(func.max(chunk.c.id)))
    return highest_id


async def evaluate_alerts(
    sessions: SessionFactory, settings: Settings, stopping: asyncio.Event
) -> EvaluationResult:
    """Compare stored forecasts against every live alert, one chunk at a time.

    A chunk that fails is rolled back, skipped and retried on the next run, so
    it cannot block the alerts after it. `stopping` is checked between chunks.
    """
    # Fetched here: `cache_logger_on_first_use` would pin a module-level logger
    # to its first configuration, out of reach of structlog's `capture_logs`.
    logger = structlog.get_logger()
    started = time.perf_counter()
    created = chunks = failed_chunks = 0
    after_id = 0

    while not stopping.is_set():
        # Read in its own step so a failing chunk still yields where to resume.
        async with sessions() as session:
            up_to_id = await next_chunk_end(
                session, after_id, settings.evaluation_chunk_size
            )
        if up_to_id is None:
            break

        chunks += 1
        try:
            statement = build_evaluation_statement(
                after_id, up_to_id, settings.enabled_channels
            )
            async with sessions.begin() as session:
                # Typed as a plain Result, but an INSERT yields a CursorResult.
                result = typing.cast(
                    CursorResult[typing.Any], await session.execute(statement)
                )
            created += result.rowcount
        except (SQLAlchemyError, TimeoutError) as exc:
            failed_chunks += 1
            # The type only: driver messages can carry connection details.
            logger.warning(
                "evaluation.chunk_failed",
                after_id=after_id,
                up_to_id=up_to_id,
                error_type=type(exc).__name__,
            )
        after_id = up_to_id

    logger.info(
        "evaluation.completed",
        created=created,
        chunks=chunks,
        failed_chunks=failed_chunks,
        duration_ms=round((time.perf_counter() - started) * 1000),
    )
    return EvaluationResult(created, chunks, failed_chunks)

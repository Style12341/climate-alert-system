import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from climate_alerts.agrobot.models import Field, WeatherForecast
from climate_alerts.alerts.models import Alert
from climate_alerts.config import Settings
from climate_alerts.db import SessionFactory
from climate_alerts.notifications import evaluation
from climate_alerts.notifications.evaluation import (
    EvaluationResult,
    build_evaluation_statement,
    evaluate_alerts,
)
from climate_alerts.notifications.models import Notification
from tests.integration.factories import (
    create_alert,
    create_field,
    create_forecast,
    create_user,
)


async def evaluate(sessions: SessionFactory, settings: Settings) -> EvaluationResult:
    return await evaluate_alerts(sessions, settings, asyncio.Event())


async def stored_notifications(sessions: SessionFactory) -> Sequence[Notification]:
    async with sessions() as session:
        statement = select(Notification).order_by(Notification.id)
        return (await session.scalars(statement)).all()


async def create_field_with_alert(
    session: AsyncSession, threshold: object = "70", timezone: str | None = None
) -> tuple[Field, Alert]:
    user = await create_user(session)
    options = {"timezone": timezone} if timezone else {}
    field = await create_field(session, user, **options)
    return field, await create_alert(session, field, threshold=threshold)


def channels(settings: Settings, names: str) -> Settings:
    return settings.model_copy(update={"notification_channels": names})


async def test_probability_equal_to_threshold_notifies(
    sessions: SessionFactory, settings: Settings
) -> None:
    async with sessions() as session:
        field, _ = await create_field_with_alert(session)
        await create_forecast(session, field, 1, "70.00")

    result = await evaluate(sessions, settings)

    assert result.created == 1


async def test_below_threshold_does_not_notify(
    sessions: SessionFactory, settings: Settings
) -> None:
    async with sessions() as session:
        field, _ = await create_field_with_alert(session)
        await create_forecast(session, field, 1, "69.99")

    result = await evaluate(sessions, settings)

    assert result.created == 0


async def test_rerun_creates_nothing(
    sessions: SessionFactory, settings: Settings
) -> None:
    async with sessions() as session:
        field, _ = await create_field_with_alert(session)
        await create_forecast(session, field, 1, "70")

    await evaluate(sessions, settings)
    second = await evaluate(sessions, settings)

    assert second.created == 0


async def test_revision_keeps_snapshot(
    sessions: SessionFactory, settings: Settings
) -> None:
    async with sessions() as session:
        field, _ = await create_field_with_alert(session)
        forecast = await create_forecast(session, field, 1, "70")

    await evaluate(sessions, settings)
    async with sessions() as session:
        revised = await session.merge(forecast)
        revised.probability = Decimal("95")
        await session.commit()
    second = await evaluate(sessions, settings)

    rows = await stored_notifications(sessions)
    assert second.created == 0
    assert [row.probability for row in rows] == [Decimal("70")]


async def test_new_date_after_notifying(
    sessions: SessionFactory, settings: Settings
) -> None:
    async with sessions() as session:
        field, _ = await create_field_with_alert(session)
        await create_forecast(session, field, 1, "70")

    await evaluate(sessions, settings)
    async with sessions() as session:
        await create_forecast(session, field, 2, "80")
    second = await evaluate(sessions, settings)

    assert second.created == 1
    assert len(await stored_notifications(sessions)) == 2


async def test_past_local_date_is_skipped(
    sessions: SessionFactory, settings: Settings
) -> None:
    # The zones are 25 hours apart, so today in Pago Pago is always already
    # over in Kiritimati, whatever time this test runs.
    async with sessions() as session:
        behind, behind_alert = await create_field_with_alert(
            session, timezone="Pacific/Pago_Pago"
        )
        ahead, _ = await create_field_with_alert(session, timezone="Pacific/Kiritimati")
        forecast = await create_forecast(session, behind, 0, "90")
        session.add(
            WeatherForecast(
                field_id=ahead.id,
                forecast_date=forecast.forecast_date,
                event_type="frost",
                probability=Decimal("90"),
            )
        )
        await session.commit()

    result = await evaluate(sessions, settings)

    rows = await stored_notifications(sessions)
    assert result.created == 1
    assert [row.alert_id for row in rows] == [behind_alert.id]


async def test_every_stored_future_date_is_evaluated(
    sessions: SessionFactory, settings: Settings
) -> None:
    async with sessions() as session:
        field, _ = await create_field_with_alert(session)
        for days_ahead in range(16):
            await create_forecast(session, field, days_ahead, "90")

    result = await evaluate(sessions, settings)

    assert result.created == 16


async def test_deleted_alert_is_skipped(
    sessions: SessionFactory, settings: Settings
) -> None:
    async with sessions() as session:
        field, alert = await create_field_with_alert(session)
        await create_forecast(session, field, 1, "90")
        alert.deleted_at = datetime.now(UTC)
        await session.commit()

    result = await evaluate(sessions, settings)

    assert result.created == 0


async def test_one_row_per_enabled_channel(
    sessions: SessionFactory, settings: Settings
) -> None:
    async with sessions() as session:
        field, _ = await create_field_with_alert(session)
        await create_forecast(session, field, 1, "90")

    result = await evaluate(sessions, channels(settings, "whatsapp,email"))

    rows = await stored_notifications(sessions)
    assert result.created == 2
    assert sorted(row.channel for row in rows) == ["email", "whatsapp"]


async def test_enabling_a_channel_adds_only_that_channel(
    sessions: SessionFactory, settings: Settings
) -> None:
    async with sessions() as session:
        field, _ = await create_field_with_alert(session)
        await create_forecast(session, field, 1, "90")

    await evaluate(sessions, channels(settings, "whatsapp"))
    second = await evaluate(sessions, channels(settings, "whatsapp,email"))

    rows = await stored_notifications(sessions)
    assert second.created == 1
    assert [row.channel for row in rows] == ["whatsapp", "email"]


async def test_chunks_cover_all_alerts(
    sessions: SessionFactory, settings: Settings
) -> None:
    async with sessions() as session:
        for _ in range(5):
            field, _ = await create_field_with_alert(session)
            await create_forecast(session, field, 1, "90")

    result = await evaluate(
        sessions, settings.model_copy(update={"evaluation_chunk_size": 2})
    )

    assert result.chunks == 3
    assert result.created == 5


async def test_failing_chunk_does_not_block_later_alerts(
    sessions: SessionFactory, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with sessions() as session:
        first_field, _ = await create_field_with_alert(session)
        second_field, second_alert = await create_field_with_alert(session)
        await create_forecast(session, first_field, 1, "90")
        await create_forecast(session, second_field, 1, "90")

    real_builder = evaluation.build_evaluation_statement
    calls = 0

    def fail_first_call(*args: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError("INSERT", {}, Exception("connection lost"))
        return real_builder(*args)

    monkeypatch.setattr(evaluation, "build_evaluation_statement", fail_first_call)

    result = await evaluate(
        sessions, settings.model_copy(update={"evaluation_chunk_size": 1})
    )

    rows = await stored_notifications(sessions)
    assert result.failed_chunks == 1
    assert result.created == 1
    assert [row.alert_id for row in rows] == [second_alert.id]


async def test_stopping_prevents_new_chunks(
    sessions: SessionFactory, settings: Settings
) -> None:
    async with sessions() as session:
        field, _ = await create_field_with_alert(session)
        await create_forecast(session, field, 1, "90")
    stopping = asyncio.Event()
    stopping.set()

    result = await evaluate_alerts(sessions, settings, stopping)

    assert result.chunks == 0
    assert result.created == 0


async def test_concurrent_evaluators_create_no_duplicates(
    sessions: SessionFactory, settings: Settings
) -> None:
    async with sessions() as session:
        for _ in range(6):
            field, _ = await create_field_with_alert(session)
            for days_ahead in range(1, 4):
                await create_forecast(session, field, days_ahead, "90")
    configured = settings.model_copy(
        update={
            "evaluation_chunk_size": 2,
            "notification_channels": "whatsapp,email",
        }
    )

    results = await asyncio.gather(*(evaluate(sessions, configured) for _ in range(4)))

    expected = 6 * 3 * 2
    rows = await stored_notifications(sessions)
    keys = {(row.alert_id, row.forecast_date, row.channel) for row in rows}
    # A deadlock would be caught as a failed chunk rather than raised.
    assert [result.failed_chunks for result in results] == [0, 0, 0, 0]
    assert sum(result.created for result in results) == expected
    assert len(rows) == expected
    assert len(keys) == expected


async def test_statement_has_no_anti_join(
    sessions: SessionFactory, settings: Settings
) -> None:
    async with sessions() as session:
        field, alert = await create_field_with_alert(session)
        await create_forecast(session, field, 1, "90")

    statement = build_evaluation_statement(0, alert.id, settings.enabled_channels)
    compiled = statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    async with sessions() as session:
        await session.execute(text("ANALYZE notifications"))
        connection = await session.connection()
        result = await connection.exec_driver_sql(f"EXPLAIN {compiled}")
        plan = "\n".join(row[0] for row in result)

    assert "Insert on notifications" in plan
    assert "Anti Join" not in plan


async def test_completed_event_reports_the_run(
    sessions: SessionFactory, settings: Settings
) -> None:
    async with sessions() as session:
        field, _ = await create_field_with_alert(session)
        await create_forecast(session, field, 1, "90")

    with capture_logs() as logs:
        await evaluate(sessions, settings)

    completed = [log for log in logs if log["event"] == "evaluation.completed"]
    assert len(completed) == 1
    assert completed[0]["created"] == 1
    assert completed[0]["chunks"] == 1
    assert completed[0]["failed_chunks"] == 0
    assert completed[0]["duration_ms"] >= 0

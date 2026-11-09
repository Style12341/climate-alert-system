from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from climate_alerts.agrobot.models import Field
from climate_alerts.alerts.models import Alert
from climate_alerts.db import SessionFactory
from climate_alerts.notifications.models import Notification
from tests.integration.factories import (
    create_alert,
    create_field,
    create_forecast,
    create_notification,
    create_user,
)


async def _field(s: AsyncSession) -> Field:
    return await create_field(s, await create_user(s))


async def _alert(s: AsyncSession) -> Alert:
    return await create_alert(s, await _field(s))


async def _hard_delete(s: AsyncSession, row: object) -> None:
    await s.delete(row)
    await s.commit()


# Each test arranges outside `pytest.raises`, so a broken setup fails the test.


async def test_rejects_threshold_zero(sessions: SessionFactory) -> None:
    async with sessions() as s:
        field = await _field(s)

        with pytest.raises(DBAPIError, match="ck_alerts_threshold_range"):
            await create_alert(s, field, threshold="0")


async def test_rejects_threshold_over_100(sessions: SessionFactory) -> None:
    async with sessions() as s:
        field = await _field(s)

        with pytest.raises(DBAPIError, match="ck_alerts_threshold_range"):
            await create_alert(s, field, threshold="100.01")


async def test_rejects_probability_over_100(sessions: SessionFactory) -> None:
    async with sessions() as s:
        field = await _field(s)

        with pytest.raises(DBAPIError, match="ck_weather_forecasts_probability_range"):
            await create_forecast(s, field, days_ahead=1, probability="101")


async def test_rejects_unknown_event_on_alert(sessions: SessionFactory) -> None:
    async with sessions() as s:
        field = await _field(s)

        with pytest.raises(
            DBAPIError, match="fk_alerts_event_type_climate_event_types"
        ):
            await create_alert(s, field, event_type="tornado")


async def test_rejects_unknown_event_on_forecast(sessions: SessionFactory) -> None:
    async with sessions() as s:
        field = await _field(s)

        with pytest.raises(
            DBAPIError, match="fk_weather_forecasts_event_type_climate_event_types"
        ):
            await create_forecast(
                s, field, days_ahead=1, probability="50", event_type="tornado"
            )


async def test_rejects_invalid_timezone(sessions: SessionFactory) -> None:
    async with sessions() as s:
        owner = await create_user(s)

        with pytest.raises(DBAPIError, match='time zone "Mars/Olympus" not recognized'):
            await create_field(s, owner, timezone="Mars/Olympus")


async def test_rejects_unknown_status(sessions: SessionFactory) -> None:
    async with sessions() as s:
        alert = await _alert(s)

        with pytest.raises(DBAPIError, match="ck_notifications_status_valid"):
            await create_notification(s, alert, status="sending")


async def test_rejects_sent_without_sent_at(sessions: SessionFactory) -> None:
    async with sessions() as s:
        alert = await _alert(s)

        with pytest.raises(DBAPIError, match="ck_notifications_sent_at"):
            await create_notification(s, alert, status="sent")


async def test_rejects_sent_at_on_pending(sessions: SessionFactory) -> None:
    async with sessions() as s:
        alert = await _alert(s)

        with pytest.raises(DBAPIError, match="ck_notifications_sent_at"):
            await create_notification(s, alert, sent_at=datetime.now(UTC))


async def test_rejects_negative_attempts(sessions: SessionFactory) -> None:
    async with sessions() as s:
        alert = await _alert(s)

        with pytest.raises(DBAPIError, match="ck_notifications_attempts_non_negative"):
            await create_notification(s, alert, attempts=-1)


async def test_rejects_duplicate_alert_date_channel(sessions: SessionFactory) -> None:
    async with sessions() as s:
        alert = await _alert(s)
        await create_notification(s, alert)

        with pytest.raises(DBAPIError, match="uq_notifications_alert_date_channel"):
            await create_notification(s, alert)


async def test_rejects_duplicate_live_alert(sessions: SessionFactory) -> None:
    async with sessions() as s:
        field = await _field(s)
        await create_alert(s, field)

        with pytest.raises(DBAPIError, match="uq_alerts_live_field_event"):
            await create_alert(s, field, threshold="80")


async def test_rejects_hard_delete_alert_with_notifications(
    sessions: SessionFactory,
) -> None:
    async with sessions() as s:
        alert = await _alert(s)
        await create_notification(s, alert)

        with pytest.raises(DBAPIError, match="fk_notifications_alert_id_alerts"):
            await _hard_delete(s, alert)


async def test_rejects_hard_delete_field_with_alerts(sessions: SessionFactory) -> None:
    async with sessions() as s:
        field = await _field(s)
        await create_alert(s, field)

        with pytest.raises(DBAPIError, match="fk_alerts_field_id_fields"):
            await _hard_delete(s, field)


async def test_soft_deleted_alert_frees_the_slot(sessions: SessionFactory) -> None:
    async with sessions() as session:
        field = await _field(session)
        alert = await create_alert(session, field)
        alert.deleted_at = datetime.now(UTC)
        await session.commit()

        replacement = await create_alert(session, field, threshold=Decimal("80"))

        assert replacement.id != alert.id


async def test_two_channels_same_date_allowed(sessions: SessionFactory) -> None:
    async with sessions() as session:
        alert = await _alert(session)

        await create_notification(session, alert, channel="whatsapp")
        await create_notification(session, alert, channel="email")

        count = await session.scalar(select(func.count()).select_from(Notification))
        assert count == 2

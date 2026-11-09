from collections.abc import Awaitable, Callable
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

type Violation = Callable[[], Awaitable[object]]


async def _field(s: AsyncSession) -> Field:
    return await create_field(s, await create_user(s))


async def _alert(s: AsyncSession) -> Alert:
    return await create_alert(s, await _field(s))


async def _threshold_zero(s: AsyncSession) -> Violation:
    field = await _field(s)
    return lambda: create_alert(s, field, threshold="0")


async def _threshold_over_100(s: AsyncSession) -> Violation:
    field = await _field(s)
    return lambda: create_alert(s, field, threshold="100.01")


async def _probability_over_100(s: AsyncSession) -> Violation:
    field = await _field(s)
    return lambda: create_forecast(s, field, days_ahead=1, probability="101")


async def _unknown_event_on_alert(s: AsyncSession) -> Violation:
    field = await _field(s)
    return lambda: create_alert(s, field, event_type="tornado")


async def _unknown_event_on_forecast(s: AsyncSession) -> Violation:
    field = await _field(s)
    return lambda: create_forecast(
        s, field, days_ahead=1, probability="50", event_type="tornado"
    )


async def _invalid_timezone(s: AsyncSession) -> Violation:
    owner = await create_user(s)
    return lambda: create_field(s, owner, timezone="Mars/Olympus")


async def _unknown_status(s: AsyncSession) -> Violation:
    alert = await _alert(s)
    return lambda: create_notification(s, alert, status="sending")


async def _sent_without_sent_at(s: AsyncSession) -> Violation:
    alert = await _alert(s)
    return lambda: create_notification(s, alert, status="sent")


async def _sent_at_on_pending(s: AsyncSession) -> Violation:
    alert = await _alert(s)
    return lambda: create_notification(s, alert, sent_at=datetime.now(UTC))


async def _negative_attempts(s: AsyncSession) -> Violation:
    alert = await _alert(s)
    return lambda: create_notification(s, alert, attempts=-1)


async def _duplicate_alert_date_channel(s: AsyncSession) -> Violation:
    alert = await _alert(s)
    await create_notification(s, alert)
    return lambda: create_notification(s, alert)


async def _duplicate_live_alert(s: AsyncSession) -> Violation:
    field = await _field(s)
    await create_alert(s, field)
    return lambda: create_alert(s, field, threshold="80")


async def _hard_delete(s: AsyncSession, row: object) -> None:
    await s.delete(row)
    await s.commit()


async def _delete_alert_with_notifications(s: AsyncSession) -> Violation:
    alert = await _alert(s)
    await create_notification(s, alert)
    return lambda: _hard_delete(s, alert)


async def _delete_field_with_alerts(s: AsyncSession) -> Violation:
    field = await _field(s)
    await create_alert(s, field)
    return lambda: _hard_delete(s, field)


@pytest.mark.parametrize(
    ("arrange", "rejected_by"),
    [
        pytest.param(_threshold_zero, "ck_alerts_threshold_range", id="threshold_zero"),
        pytest.param(
            _threshold_over_100, "ck_alerts_threshold_range", id="threshold_over_100"
        ),
        pytest.param(
            _probability_over_100,
            "ck_weather_forecasts_probability_range",
            id="probability_over_100",
        ),
        pytest.param(
            _unknown_event_on_alert,
            "fk_alerts_event_type_climate_event_types",
            id="unknown_event_on_alert",
        ),
        pytest.param(
            _unknown_event_on_forecast,
            "fk_weather_forecasts_event_type_climate_event_types",
            id="unknown_event_on_forecast",
        ),
        pytest.param(_invalid_timezone, "Mars/Olympus", id="invalid_timezone"),
        pytest.param(
            _unknown_status, "ck_notifications_status_valid", id="unknown_status"
        ),
        pytest.param(
            _sent_without_sent_at, "ck_notifications_sent_at", id="sent_without_sent_at"
        ),
        pytest.param(
            _sent_at_on_pending, "ck_notifications_sent_at", id="sent_at_on_pending"
        ),
        pytest.param(
            _negative_attempts,
            "ck_notifications_attempts_non_negative",
            id="negative_attempts",
        ),
        pytest.param(
            _duplicate_alert_date_channel,
            "uq_notifications_alert_date_channel",
            id="duplicate_alert_date_channel",
        ),
        pytest.param(
            _duplicate_live_alert,
            "uq_alerts_live_field_event",
            id="duplicate_live_alert",
        ),
        pytest.param(
            _delete_alert_with_notifications,
            "fk_notifications_alert_id_alerts",
            id="hard_delete_alert_with_notifications",
        ),
        pytest.param(
            _delete_field_with_alerts,
            "fk_alerts_field_id_fields",
            id="hard_delete_field_with_alerts",
        ),
    ],
)
async def test_rejected_data(
    sessions: SessionFactory,
    arrange: Callable[[AsyncSession], Awaitable[Violation]],
    rejected_by: str,
) -> None:
    async with sessions() as session:
        # Arranged outside pytest.raises: a broken setup must fail the test.
        violate = await arrange(session)

        with pytest.raises(DBAPIError, match=rejected_by):
            await violate()


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

"""Shared helpers for integration tests, in two groups.

Row factories create users, fields, alerts, forecasts and notifications. Each
factory commits, so the row is visible to other sessions, as it would be in
production. Numeric arguments accept anything `Decimal(str(value))` can parse.

Dispatch scaffolding (`LEASE_SECONDS`, `FakeSender`, `registry`, `dispatch`,
`stored`, `expire_leases`) drives the dispatcher in test_dispatch and test_claim.
"""

import asyncio
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import count
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from climate_alerts.agrobot.models import Field, User, WeatherForecast
from climate_alerts.alerts.models import Alert
from climate_alerts.config import Settings
from climate_alerts.db import SessionFactory
from climate_alerts.notifications.dispatch import DispatchResult, dispatch_due
from climate_alerts.notifications.models import Notification, NotificationStatus
from climate_alerts.notifications.senders import (
    NotificationSender,
    OutboundNotification,
)

# --- Row factories ---

_phone_numbers = count(1)


async def create_user(
    s: AsyncSession, phone: str | None = None, email: str | None = None
) -> User:
    user = User(phone=phone or f"+54911{next(_phone_numbers):08d}", email=email)
    s.add(user)
    await s.commit()
    return user


async def create_field(
    s: AsyncSession,
    owner: User,
    name: str = "Test field",
    timezone: str = "America/Argentina/Buenos_Aires",
) -> Field:
    field = Field(owner_id=owner.id, name=name, timezone=timezone)
    s.add(field)
    await s.commit()
    return field


async def create_alert(
    s: AsyncSession,
    field: Field,
    event_type: str = "frost",
    threshold: object = "70",
) -> Alert:
    alert = Alert(
        field_id=field.id,
        event_type=event_type,
        threshold=Decimal(str(threshold)),
    )
    s.add(alert)
    await s.commit()
    return alert


async def create_forecast(
    s: AsyncSession,
    field: Field,
    days_ahead: int,
    probability: object,
    event_type: str = "frost",
) -> WeatherForecast:
    forecast = WeatherForecast(
        field_id=field.id,
        forecast_date=_local_date(field, days_ahead),
        event_type=event_type,
        probability=Decimal(str(probability)),
    )
    s.add(forecast)
    await s.commit()
    return forecast


async def create_notification(
    s: AsyncSession,
    alert: Alert,
    days_ahead: int = 1,
    channel: str = "whatsapp",
    **overrides: Any,
) -> Notification:
    field = await s.get_one(Field, alert.field_id)
    values: dict[str, Any] = {
        "alert_id": alert.id,
        "forecast_date": _local_date(field, days_ahead),
        "channel": channel,
        "probability": Decimal("90"),
        "threshold": alert.threshold,
    }
    notification = Notification(**(values | overrides))
    s.add(notification)
    await s.commit()
    return notification


def _local_date(field: Field, days_ahead: int) -> date:
    return datetime.now(ZoneInfo(field.timezone)).date() + timedelta(days=days_ahead)


# --- Dispatch scaffolding, shared by test_dispatch and test_claim ---

LEASE_SECONDS = 300


class FakeSender:
    """Records every call, then fails or hangs as configured."""

    def __init__(
        self,
        channel: str = "whatsapp",
        error: Exception | None = None,
        hang_ids: frozenset[int] = frozenset(),
    ) -> None:
        self.channel = channel
        self.error = error
        self.hang_ids = hang_ids
        self.calls: list[int] = []

    async def send(self, notification: OutboundNotification) -> None:
        self.calls.append(notification.notification_id)
        if notification.notification_id in self.hang_ids:
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error


def registry(*senders: NotificationSender) -> dict[str, NotificationSender]:
    return {sender.channel: sender for sender in senders}


async def create_owned_alert(session: AsyncSession, email: str | None = None) -> Alert:
    """Create a user, a field named "La Esperanza" and a default alert on it."""
    user = await create_user(session, email=email)
    field = await create_field(session, user, name="La Esperanza")
    return await create_alert(session, field)


async def create_due_notifications(
    sessions: SessionFactory, n: int, **overrides: Any
) -> list[int]:
    """Create `n` pending rows, due now because `next_attempt_at` defaults to now()."""
    async with sessions() as session:
        alert = await create_owned_alert(session)
        created = [
            await create_notification(session, alert, days_ahead=day, **overrides)
            for day in range(n)
        ]
    return [notification.id for notification in created]


async def stored(sessions: SessionFactory, notification_id: int) -> Notification:
    async with sessions() as session:
        return await session.get_one(Notification, notification_id)


async def expire_leases(sessions: SessionFactory) -> None:
    """Make every pending row due, whether it is leased or not."""
    async with sessions.begin() as session:
        await session.execute(
            update(Notification)
            .where(Notification.status == NotificationStatus.PENDING.value)
            .values(next_attempt_at=func.now() - timedelta(seconds=1))
        )


async def dispatch(
    sessions: SessionFactory,
    senders: Mapping[str, NotificationSender],
    settings: Settings,
) -> DispatchResult:
    return await dispatch_due(sessions, senders, settings, asyncio.Event())

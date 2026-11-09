"""Row factories for integration tests.

Each one commits, so the row is visible to other sessions, as it would be in
production. Numeric arguments accept anything `Decimal(str(value))` can parse.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import count
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from climate_alerts.agrobot.models import Field, User, WeatherForecast
from climate_alerts.alerts.models import Alert
from climate_alerts.notifications.models import Notification

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

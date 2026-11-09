from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncSession

from climate_alerts.agrobot.models import Field
from climate_alerts.alerts.models import Alert
from climate_alerts.notifications.models import Notification, NotificationStatus


async def list_notifications(
    session: AsyncSession,
    user_id: int,
    *,
    page_size: int,
    cursor: int | None,
    field_id: int | None,
    status: NotificationStatus | None,
    channel: str | None,
) -> tuple[Sequence[Row[*tuple[Any, ...]]], int | None]:
    """Return one page, newest first, and the cursor of the next page if any."""
    # Soft-deleted alerts are deliberately not excluded: their history remains.
    statement = (
        select(
            Notification.id,
            Notification.alert_id,
            Alert.field_id,
            Alert.event_type,
            Notification.forecast_date,
            Notification.channel,
            Notification.probability,
            Notification.threshold,
            Notification.status,
            Notification.attempts,
            Notification.last_error,
            Notification.created_at,
            Notification.sent_at,
        )
        .join(Alert, Notification.alert_id == Alert.id)
        .join(Field, Alert.field_id == Field.id)
        .where(Field.owner_id == user_id)
        .order_by(Notification.id.desc())
        .limit(page_size + 1)
    )
    if cursor is not None:
        statement = statement.where(Notification.id < cursor)
    if field_id is not None:
        statement = statement.where(Alert.field_id == field_id)
    if status is not None:
        statement = statement.where(Notification.status == status.value)
    if channel is not None:
        statement = statement.where(Notification.channel == channel)

    rows = (await session.execute(statement)).all()
    page = rows[:page_size]
    return page, page[-1].id if len(rows) > page_size else None

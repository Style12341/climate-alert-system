from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from climate_alerts.agrobot.models import Field
from climate_alerts.alerts.models import Alert
from climate_alerts.api.dependencies import (
    MAX_BIGINT,
    CurrentUserId,
    SessionDep,
    SettingsDep,
)
from climate_alerts.notifications.models import Notification, NotificationStatus
from climate_alerts.notifications.schemas import NotificationOut, NotificationPage

router = APIRouter()

# The upper bound lives in Settings, so the route checks it at runtime.
Limit = Annotated[int | None, Query(gt=0)]
Cursor = Annotated[int | None, Query(gt=0, le=MAX_BIGINT)]
FieldFilter = Annotated[int | None, Query(gt=0, le=MAX_BIGINT)]
StatusFilter = Annotated[NotificationStatus | None, Query(alias="status")]
# PostgreSQL text cannot hold NUL, so the comparison would raise a 500.
ChannelFilter = Annotated[str | None, Query(pattern=r"^[^\x00]*$")]


@router.get("/notifications", response_model=NotificationPage)
async def list_notifications(
    session: SessionDep,
    settings: SettingsDep,
    user_id: CurrentUserId,
    limit: Limit = None,
    cursor: Cursor = None,
    field_id: FieldFilter = None,
    status_filter: StatusFilter = None,
    channel: ChannelFilter = None,
) -> NotificationPage:
    page_size = settings.default_page_size if limit is None else limit
    if page_size > settings.max_page_size:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"limit must be at most {settings.max_page_size}",
        )

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
    if status_filter is not None:
        statement = statement.where(Notification.status == status_filter.value)
    if channel is not None:
        statement = statement.where(Notification.channel == channel)

    rows = (await session.execute(statement)).all()
    page = rows[:page_size]
    return NotificationPage(
        items=[NotificationOut.model_validate(row) for row in page],
        next_cursor=page[-1].id if len(rows) > page_size else None,
    )

from typing import Annotated

from fastapi import APIRouter, Query

from climate_alerts.api.dependencies import (
    NO_NUL_PATTERN,
    CurrentUserId,
    OptionalIdQuery,
    SessionDep,
)
from climate_alerts.api.errors import error_responses
from climate_alerts.errors import InvalidCredentials
from climate_alerts.notifications import service
from climate_alerts.notifications.models import NotificationStatus
from climate_alerts.notifications.schemas import NotificationOut, NotificationPage

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

router = APIRouter(
    tags=["notifications"], responses=error_responses(InvalidCredentials)
)

Limit = Annotated[int, Query(gt=0, le=MAX_PAGE_SIZE)]
StatusFilter = Annotated[NotificationStatus | None, Query(alias="status")]
ChannelFilter = Annotated[str | None, Query(pattern=NO_NUL_PATTERN)]


@router.get("/notifications", response_model=NotificationPage)
async def list_notifications(
    session: SessionDep,
    user_id: CurrentUserId,
    limit: Limit = DEFAULT_PAGE_SIZE,
    cursor: OptionalIdQuery = None,
    field_id: OptionalIdQuery = None,
    status_filter: StatusFilter = None,
    channel: ChannelFilter = None,
) -> NotificationPage:
    rows, next_cursor = await service.list_notifications(
        session,
        user_id,
        page_size=limit,
        cursor=cursor,
        field_id=field_id,
        status=status_filter,
        channel=channel,
    )
    return NotificationPage(
        items=[NotificationOut.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )

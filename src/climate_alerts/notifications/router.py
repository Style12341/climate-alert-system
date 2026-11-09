from typing import Annotated

from fastapi import APIRouter, Query

from climate_alerts.api.dependencies import (
    NO_NUL_PATTERN,
    CurrentUserId,
    OptionalIdQuery,
    SessionDep,
    SettingsDep,
    invalid_request,
)
from climate_alerts.api.errors import error_responses
from climate_alerts.errors import InvalidCredentials
from climate_alerts.notifications import service
from climate_alerts.notifications.models import NotificationStatus
from climate_alerts.notifications.schemas import NotificationOut, NotificationPage

router = APIRouter(
    tags=["notifications"], responses=error_responses(InvalidCredentials)
)

# The upper bound lives in Settings, so the route checks it at runtime.
Limit = Annotated[int | None, Query(gt=0)]
StatusFilter = Annotated[NotificationStatus | None, Query(alias="status")]
ChannelFilter = Annotated[str | None, Query(pattern=NO_NUL_PATTERN)]


@router.get("/notifications", response_model=NotificationPage)
async def list_notifications(
    session: SessionDep,
    settings: SettingsDep,
    user_id: CurrentUserId,
    limit: Limit = None,
    cursor: OptionalIdQuery = None,
    field_id: OptionalIdQuery = None,
    status_filter: StatusFilter = None,
    channel: ChannelFilter = None,
) -> NotificationPage:
    page_size = settings.default_page_size if limit is None else limit
    if page_size > settings.max_page_size:
        raise invalid_request(
            ("query", "limit"),
            "limit_too_large",
            f"Limit must be at most {settings.max_page_size}",
        )

    rows, next_cursor = await service.list_notifications(
        session,
        user_id,
        page_size=page_size,
        cursor=cursor,
        field_id=field_id,
        status=status_filter,
        channel=channel,
    )
    return NotificationPage(
        items=[NotificationOut.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )

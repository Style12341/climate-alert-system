from collections.abc import Sequence

from fastapi import APIRouter
from sqlalchemy import select

from climate_alerts.agrobot.models import ClimateEventType
from climate_alerts.agrobot.schemas import EventTypeOut
from climate_alerts.api.dependencies import CurrentUserId, SessionDep

router = APIRouter()


@router.get("/climate-event-types", response_model=list[EventTypeOut])
async def list_climate_event_types(
    session: SessionDep, _user_id: CurrentUserId
) -> Sequence[ClimateEventType]:
    result = await session.scalars(
        select(ClimateEventType).order_by(ClimateEventType.code)
    )
    return result.all()

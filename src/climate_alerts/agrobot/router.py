from fastapi import APIRouter

from climate_alerts.agrobot import service
from climate_alerts.agrobot.models import ClimateEventType
from climate_alerts.agrobot.schemas import EventTypeOut
from climate_alerts.api.dependencies import CurrentUserId, SessionDep
from climate_alerts.api.schemas import UNAUTHORIZED

router = APIRouter(tags=["catalog"], responses=UNAUTHORIZED)


@router.get("/climate-event-types", response_model=list[EventTypeOut])
async def list_climate_event_types(
    session: SessionDep, _user_id: CurrentUserId
) -> list[ClimateEventType]:
    return await service.list_climate_event_types(session)

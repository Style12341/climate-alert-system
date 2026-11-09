from fastapi import APIRouter

from climate_alerts.agrobot import service
from climate_alerts.agrobot.models import ClimateEventType
from climate_alerts.agrobot.schemas import EventTypeOut
from climate_alerts.api.dependencies import CurrentUserId, SessionDep
from climate_alerts.api.errors import error_responses
from climate_alerts.errors import InvalidCredentials

router = APIRouter(tags=["catalog"], responses=error_responses(InvalidCredentials))


@router.get("/climate-event-types", response_model=list[EventTypeOut])
async def list_climate_event_types(
    session: SessionDep, _user_id: CurrentUserId
) -> list[ClimateEventType]:
    return await service.list_climate_event_types(session)

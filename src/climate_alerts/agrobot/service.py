from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from climate_alerts.agrobot.models import ClimateEventType


async def list_climate_event_types(session: AsyncSession) -> list[ClimateEventType]:
    statement = select(ClimateEventType).order_by(ClimateEventType.code)
    return list(await session.scalars(statement))

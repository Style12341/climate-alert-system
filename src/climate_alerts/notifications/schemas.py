from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from climate_alerts.errors import DeliveryFailure
from climate_alerts.notifications.models import NotificationStatus

LAST_ERROR_DESCRIPTION = (
    "Why the last delivery attempt failed, null once sent. Always one of: "
    + "; ".join(failure.message for failure in DeliveryFailure)
    + ". Internal errors are logged, never returned."
)


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    alert_id: int
    field_id: int
    event_type: str
    forecast_date: date
    channel: str
    probability: Decimal
    threshold: Decimal
    status: NotificationStatus
    attempts: int
    last_error: Annotated[str | None, Field(description=LAST_ERROR_DESCRIPTION)]
    created_at: datetime
    sent_at: datetime | None


class NotificationPage(BaseModel):
    items: list[NotificationOut]
    next_cursor: int | None

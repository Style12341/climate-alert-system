from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from climate_alerts.notifications.models import NotificationStatus


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
    last_error: str | None
    created_at: datetime
    sent_at: datetime | None


class NotificationPage(BaseModel):
    items: list[NotificationOut]
    next_cursor: int | None

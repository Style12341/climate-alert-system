from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from climate_alerts.api.dependencies import NO_NUL_PATTERN

Threshold = Annotated[Decimal, Field(gt=0, le=100, max_digits=5, decimal_places=2)]


class AlertCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: str = Field(pattern=NO_NUL_PATTERN)
    threshold: Threshold


class AlertUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    threshold: Threshold


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    field_id: int
    event_type: str
    threshold: Decimal
    created_at: datetime
    updated_at: datetime

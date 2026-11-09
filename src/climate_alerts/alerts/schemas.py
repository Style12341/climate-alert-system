from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

Threshold = Annotated[Decimal, Field(gt=0, le=100, max_digits=5, decimal_places=2)]


class AlertCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # PostgreSQL text cannot hold NUL, so the catalog lookup would raise a 500.
    event_type: str = Field(pattern=r"^[^\x00]*$")
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

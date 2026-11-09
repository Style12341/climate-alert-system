from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    column,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from climate_alerts.agrobot.models import ClimateEventType, Field
from climate_alerts.db import Base


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        CheckConstraint("threshold > 0 AND threshold <= 100", name="threshold_range"),
        Index(
            "uq_alerts_live_field_event",
            "field_id",
            "event_type",
            unique=True,
            postgresql_where=column("deleted_at").is_(None),
        ),
        # The partial index above skips deleted alerts, and the notification
        # history reads them too.
        Index("ix_alerts_field_id", "field_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    field_id: Mapped[int] = mapped_column(ForeignKey(Field.id, ondelete="RESTRICT"))
    event_type: Mapped[str] = mapped_column(
        ForeignKey(ClimateEventType.code, ondelete="RESTRICT")
    )
    threshold: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    column,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from climate_alerts.alerts.models import Alert
from climate_alerts.db import Base


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint(
            "alert_id",
            "forecast_date",
            "channel",
            name="uq_notifications_alert_date_channel",
        ),
        CheckConstraint("status IN ('pending', 'sent', 'failed')", name="status_valid"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("(status = 'sent') = (sent_at IS NOT NULL)", name="sent_at"),
        Index(
            "ix_notifications_due",
            "next_attempt_at",
            "id",
            postgresql_where=column("status") == NotificationStatus.PENDING.value,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey(Alert.id, ondelete="RESTRICT"))
    forecast_date: Mapped[date] = mapped_column(Date)
    channel: Mapped[str] = mapped_column(Text)
    probability: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    threshold: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    # Plain text: the CHECK owns the valid values. See ModeloDeDatos.md.
    status: Mapped[str] = mapped_column(
        Text, server_default=NotificationStatus.PENDING.value
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

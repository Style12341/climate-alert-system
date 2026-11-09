from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Numeric,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from climate_alerts.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    phone: Mapped[str] = mapped_column(Text, unique=True)
    email: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Field(Base):
    __tablename__ = "fields"
    __table_args__ = (
        CheckConstraint(
            "(TIMESTAMP '2000-01-01 00:00' AT TIME ZONE timezone) IS NOT NULL",
            name="timezone_valid",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey(User.id, ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ClimateEventType(Base):
    __tablename__ = "climate_event_types"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    label: Mapped[str] = mapped_column(Text)


class WeatherForecast(Base):
    __tablename__ = "weather_forecasts"
    __table_args__ = (
        CheckConstraint("probability BETWEEN 0 AND 100", name="probability_range"),
    )

    field_id: Mapped[int] = mapped_column(
        ForeignKey(Field.id, ondelete="CASCADE"), primary_key=True
    )
    forecast_date: Mapped[date] = mapped_column(Date, primary_key=True)
    event_type: Mapped[str] = mapped_column(
        ForeignKey(ClimateEventType.code, ondelete="RESTRICT"), primary_key=True
    )
    probability: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

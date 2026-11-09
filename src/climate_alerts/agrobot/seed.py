"""Demo data for a reviewer: `python -m climate_alerts.agrobot.seed`.

Safe to run repeatedly. It does not reset anything: forecasts from earlier days
stay in place, and alerts or fields changed since the last run are left alone.
"""

import asyncio
import random
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, insert, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from climate_alerts.agrobot.models import (
    ClimateEventType,
    Field,
    User,
    WeatherForecast,
)
from climate_alerts.agrobot.tokens import issue_token
from climate_alerts.alerts.models import Alert
from climate_alerts.config import Settings, get_settings
from climate_alerts.db import SessionFactory, build_engine, build_session_factory

_FORECAST_DAYS = 16
_RANDOM_SEED = 20240229
_MAX_BACKGROUND_HUNDREDTHS = 4000

_BUENOS_AIRES = "Buenos Aires"
_MONTEVIDEO = "Montevideo"


@dataclass(frozen=True)
class _SeedUser:
    phone: str
    email: str | None


@dataclass(frozen=True)
class _SeedField:
    name: str
    timezone: str
    owner: int  # Index into _USERS.


# The order is a contract: the API collection uses the second user, who has no
# email, both as the "other user" and to see `failed` rows.
_USERS = (
    _SeedUser("+5491100000001", "productor.uno@example.com"),
    _SeedUser("+59890000002", None),
    _SeedUser("+525500000003", "productor.tres@example.com"),
)
_FIELDS = (
    _SeedField(_BUENOS_AIRES, "America/Argentina/Buenos_Aires", owner=0),
    _SeedField(_MONTEVIDEO, "America/Montevideo", owner=1),
    _SeedField("Bogotá", "America/Bogota", owner=0),
    _SeedField("Mexico City", "America/Mexico_City", owner=2),
    _SeedField("São Paulo", "America/Sao_Paulo", owner=0),
)

_SCENARIO_EVENT = "frost"
# Field name and days from its local today. Everything else stays at or below
# 40, so these are the only two forecasts that can reach an alert.
_SCENARIO = {
    (_BUENOS_AIRES, 1): Decimal(85),
    (_MONTEVIDEO, 2): Decimal(90),
}

_FROST_THRESHOLD = Decimal(70)
_RAIN_THRESHOLD = Decimal(80)
# On a scenario field on purpose: its frost at 85 is above this threshold, so
# a notification here would mean evaluation ignored the event type.
_RAIN_FIELD = _BUENOS_AIRES


@dataclass(frozen=True)
class SeededUser:
    id: int
    field_ids: list[int]
    token: str
    has_email: bool


def _background_probability(field_name: str, day: date, event: str) -> Decimal:
    """Return the same value for a field, date and event on every run.

    One generator per key, because a shared one would change every value
    whenever the run starts on another day. Keyed by name since ids vary
    between databases.
    """
    key = f"{_RANDOM_SEED}:{field_name}:{day.isoformat()}:{event}"
    generator = random.Random(key)  # noqa: S311 - demo data, not security
    return Decimal(generator.randint(0, _MAX_BACKGROUND_HUNDREDTHS)) / 100


def _forecast_rows(
    field_id: int, spec: _SeedField, first_day: date, events: list[str]
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for offset in range(_FORECAST_DAYS):
        day = first_day + timedelta(days=offset)
        for event in events:
            scenario = (
                _SCENARIO.get((spec.name, offset)) if event == _SCENARIO_EVENT else None
            )
            probability = (
                _background_probability(spec.name, day, event)
                if scenario is None
                else scenario
            )
            rows.append(
                {
                    "field_id": field_id,
                    "forecast_date": day,
                    "event_type": event,
                    "probability": probability,
                }
            )
    return rows


async def _upsert_user(session: AsyncSession, spec: _SeedUser) -> int:
    insert_user = postgresql.insert(User).values(phone=spec.phone, email=spec.email)
    statement = insert_user.on_conflict_do_update(
        index_elements=[User.phone],
        set_={"email": insert_user.excluded.email},
    ).returning(User.id)
    return (await session.execute(statement)).scalar_one()


async def _find_or_create_field(
    session: AsyncSession, owner_id: int, spec: _SeedField
) -> int:
    # No unique constraint on fields, so ON CONFLICT has nothing to target.
    existing = await session.scalar(
        select(Field.id).where(Field.owner_id == owner_id, Field.name == spec.name)
    )
    if existing is not None:
        return existing
    statement = (
        insert(Field)
        .values(owner_id=owner_id, name=spec.name, timezone=spec.timezone)
        .returning(Field.id)
    )
    return (await session.execute(statement)).scalar_one()


async def _ensure_alert(
    session: AsyncSession, field_id: int, event: str, threshold: Decimal
) -> None:
    await session.execute(
        postgresql.insert(Alert)
        .values(field_id=field_id, event_type=event, threshold=threshold)
        .on_conflict_do_nothing(
            index_elements=[Alert.field_id, Alert.event_type],
            index_where=Alert.deleted_at.is_(None),
        )
    )


async def _upsert_forecasts(
    session: AsyncSession, rows: list[dict[str, object]]
) -> None:
    statement = postgresql.insert(WeatherForecast).values(rows)
    await session.execute(
        statement.on_conflict_do_update(
            constraint="pk_weather_forecasts",
            set_={
                "probability": statement.excluded.probability,
                "fetched_at": func.now(),
            },
        )
    )


async def seed(
    sessions: SessionFactory, settings: Settings, today: date | None = None
) -> list[SeededUser]:
    """Create or refresh the demo data in one transaction.

    `today` replaces every field's local date, for tests.
    """
    field_ids: list[list[int]] = [[] for _ in _USERS]
    user_ids: list[int] = []

    async with sessions.begin() as session:
        events = list(
            await session.scalars(
                select(ClimateEventType.code).order_by(ClimateEventType.code)
            )
        )
        # One statement each: the ids must follow the order of _USERS.
        for user in _USERS:
            user_ids.append(await _upsert_user(session, user))

        forecasts: list[dict[str, object]] = []
        for spec in _FIELDS:
            field_id = await _find_or_create_field(session, user_ids[spec.owner], spec)
            field_ids[spec.owner].append(field_id)
            await _ensure_alert(session, field_id, _SCENARIO_EVENT, _FROST_THRESHOLD)
            if spec.name == _RAIN_FIELD:
                await _ensure_alert(session, field_id, "rain", _RAIN_THRESHOLD)
            first_day = (
                datetime.now(ZoneInfo(spec.timezone)).date() if today is None else today
            )
            forecasts.extend(_forecast_rows(field_id, spec, first_day, events))

        await _upsert_forecasts(session, forecasts)

    ttl = timedelta(hours=settings.seed_token_ttl_hours)
    return [
        SeededUser(
            id=user_id,
            field_ids=owned,
            token=issue_token(user_id, settings, ttl),
            has_email=spec.email is not None,
        )
        for user_id, owned, spec in zip(user_ids, field_ids, _USERS, strict=True)
    ]


async def _run(settings: Settings) -> list[SeededUser]:
    engine = build_engine(settings)
    try:
        return await seed(build_session_factory(engine), settings)
    finally:
        await engine.dispose()


def main() -> None:
    settings = get_settings()
    try:
        users = asyncio.run(_run(settings))
    except Exception as exc:
        # The type only: SQLAlchemy errors quote bound parameters, which here
        # include phones and emails, and driver errors can carry the host.
        print(f"seed failed: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)

    for user in users:
        fields = ",".join(str(field_id) for field_id in user.field_ids)
        print(f"user {user.id} fields {fields} token {user.token}")


if __name__ == "__main__":
    main()

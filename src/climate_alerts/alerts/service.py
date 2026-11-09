from decimal import Decimal

from sqlalchemy import ColumnElement, and_, func, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from climate_alerts.agrobot.models import ClimateEventType, Field
from climate_alerts.alerts.models import Alert


class FieldNotFound(Exception):
    pass


class AlertNotFound(Exception):
    pass


class UnknownEventType(Exception):
    pass


class AlertAlreadyExists(Exception):
    pass


def _live_alert_owned_by(user_id: int) -> ColumnElement[bool]:
    return and_(
        Alert.deleted_at.is_(None),
        Alert.field_id == Field.id,
        Field.owner_id == user_id,
    )


async def create_alert(
    session: AsyncSession,
    user_id: int,
    field_id: int,
    event_type: str,
    threshold: Decimal,
) -> Alert:
    owned_field = await session.scalar(
        select(Field.id).where(Field.id == field_id, Field.owner_id == user_id)
    )
    if owned_field is None:
        raise FieldNotFound

    known_event = await session.scalar(
        select(ClimateEventType.code).where(ClimateEventType.code == event_type)
    )
    if known_event is None:
        raise UnknownEventType

    # Arbitrates through the partial unique index, so two concurrent creates
    # resolve to one row and one empty result without parsing driver errors.
    alert: Alert | None = await session.scalar(
        postgresql.insert(Alert)
        .values(field_id=field_id, event_type=event_type, threshold=threshold)
        .on_conflict_do_nothing(
            index_elements=["field_id", "event_type"],
            index_where=Alert.deleted_at.is_(None),
        )
        .returning(Alert)
    )
    if alert is None:
        raise AlertAlreadyExists
    await session.commit()
    return alert


async def list_alerts(
    session: AsyncSession, user_id: int, field_id: int | None
) -> list[Alert]:
    statement = select(Alert).where(_live_alert_owned_by(user_id)).order_by(Alert.id)
    if field_id is not None:
        statement = statement.where(Alert.field_id == field_id)
    return list(await session.scalars(statement))


async def get_alert(session: AsyncSession, user_id: int, alert_id: int) -> Alert:
    alert = await session.scalar(
        select(Alert).where(Alert.id == alert_id, _live_alert_owned_by(user_id))
    )
    if alert is None:
        raise AlertNotFound
    return alert


async def update_threshold(
    session: AsyncSession, user_id: int, alert_id: int, threshold: Decimal
) -> Alert:
    alert: Alert | None = await session.scalar(
        update(Alert)
        .where(Alert.id == alert_id, _live_alert_owned_by(user_id))
        # updated_at has only a server default, so it must be set here.
        .values(threshold=threshold, updated_at=func.now())
        .returning(Alert)
    )
    if alert is None:
        raise AlertNotFound
    await session.commit()
    return alert


async def delete_alert(session: AsyncSession, user_id: int, alert_id: int) -> None:
    deleted_id = await session.scalar(
        update(Alert)
        .where(Alert.id == alert_id, _live_alert_owned_by(user_id))
        .values(deleted_at=func.now())
        .returning(Alert.id)
    )
    if deleted_id is None:
        raise AlertNotFound
    await session.commit()

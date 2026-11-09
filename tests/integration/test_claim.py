import asyncio
from collections import Counter
from datetime import timedelta
from decimal import Decimal
from typing import Literal

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects import postgresql

from climate_alerts.config import Settings
from climate_alerts.db import SessionFactory
from climate_alerts.errors import DeliveryFailure
from climate_alerts.notifications.dispatch import (
    DispatchResult,
    Outcome,
    build_claim_statement,
    claim_batch,
    record_outcomes,
)
from climate_alerts.notifications.models import Notification
from climate_alerts.notifications.senders import PermanentDeliveryError
from tests.integration.factories import (
    LEASE_SECONDS,
    FakeSender,
    create_due_notifications,
    create_notification,
    create_owned_alert,
    dispatch,
    expire_leases,
    registry,
    stored,
)


async def explain_claim(sessions: SessionFactory, batch_size: int) -> str:
    statement = build_claim_statement(batch_size, LEASE_SECONDS)
    compiled = statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    async with sessions() as session:
        connection = await session.connection()
        result = await connection.exec_driver_sql(f"EXPLAIN {compiled}")
        return "\n".join(row[0] for row in result)


async def test_settled_rows_are_never_claimed_again(
    sessions: SessionFactory, settings: Settings
) -> None:
    async with sessions() as session:
        alert = await create_owned_alert(session)
        sent = [
            await create_notification(session, alert, days_ahead=day)
            for day in range(2)
        ]
        failed = await create_notification(session, alert, channel="email")
    whatsapp_sender = FakeSender("whatsapp")
    email_sender = FakeSender(
        "email", error=PermanentDeliveryError(DeliveryFailure.NO_ADDRESS)
    )
    senders = registry(whatsapp_sender, email_sender)
    await dispatch(sessions, senders, settings)
    # Every row due again, whatever its status.
    async with sessions.begin() as session:
        await session.execute(
            update(Notification).values(
                next_attempt_at=func.now() - timedelta(seconds=1)
            )
        )

    result = await dispatch(sessions, senders, settings)

    assert result == DispatchResult(claimed=0, sent=0, retried=0, failed=0)
    assert sorted(whatsapp_sender.calls) == [row.id for row in sent]
    assert email_sender.calls == [failed.id]
    for row in sent:
        assert (await stored(sessions, row.id)).status == "sent"
    assert (await stored(sessions, failed.id)).status == "failed"


async def test_claim_respects_batch_size_and_order(
    sessions: SessionFactory,
) -> None:
    newest, middle, oldest = await create_due_notifications(sessions, 3)
    async with sessions.begin() as session:
        for notification_id, age in ((newest, 10), (middle, 20), (oldest, 30)):
            await session.execute(
                update(Notification)
                .where(Notification.id == notification_id)
                .values(next_attempt_at=func.now() - timedelta(seconds=age))
            )

    claimed = await claim_batch(sessions, 2, LEASE_SECONDS)

    # RETURNING has no order, and the old due time is gone by then: what the
    # ordering guarantees is which rows the batch takes.
    assert {item.notification.notification_id for item in claimed} == {oldest, middle}
    assert all(item.attempts == 1 for item in claimed)


async def test_claim_returns_what_a_sender_needs(sessions: SessionFactory) -> None:
    async with sessions() as session:
        alert = await create_owned_alert(session, email="maria@example.com")
        notification = await create_notification(session, alert, channel="email")

    [claimed] = await claim_batch(sessions, 10, LEASE_SECONDS)

    outbound = claimed.notification
    assert outbound.notification_id == notification.id
    assert outbound.channel == "email"
    assert outbound.recipient.email == "maria@example.com"
    assert outbound.recipient.phone.startswith("+")
    assert outbound.field_name == "La Esperanza"
    assert outbound.event_type == "frost"
    assert outbound.forecast_date == notification.forecast_date
    assert outbound.probability == notification.probability
    assert outbound.threshold == notification.threshold


async def test_claim_stays_within_batch_size_with_stale_statistics(
    sessions: SessionFactory,
) -> None:
    """Statistics say one row, so the planner would re-run a plain IN subquery.

    Each re-run skips the rows this command already updated, so the LIMIT slides
    forward and bounds nothing. The 21 rows must share a heap page: the planner
    scales its estimate by page count.
    """
    await create_due_notifications(sessions, 1)
    async with sessions.begin() as session:
        await session.execute(text("ANALYZE notifications"))
    await create_due_notifications(sessions, 20)

    claimed = await claim_batch(sessions, 5, LEASE_SECONDS)

    assert len(claimed) == 5


async def test_claim_updates_by_primary_key(sessions: SessionFactory) -> None:
    """With healthy statistics the claimed rows are found through the primary key.

    A form the planner turns into a hash semi join scans the whole table on
    every claim instead.
    """
    async with sessions() as session:
        alert = await create_owned_alert(session)
        today = (await session.execute(select(func.current_date()))).scalar_one()
        await session.execute(
            insert(Notification),
            [
                {
                    "alert_id": alert.id,
                    "forecast_date": today + timedelta(days=day),
                    "channel": "whatsapp",
                    "probability": Decimal("90"),
                    "threshold": alert.threshold,
                }
                for day in range(3000)
            ],
        )
        await session.commit()
    async with sessions.begin() as session:
        await session.execute(text("ANALYZE notifications"))

    plan = await explain_claim(sessions, 50)

    assert "Index Scan using pk_notifications on notifications" in plan
    assert "Seq Scan on notifications" not in plan


async def test_leased_rows_are_invisible(sessions: SessionFactory) -> None:
    await create_due_notifications(sessions, 2)

    first = await claim_batch(sessions, 10, LEASE_SECONDS)
    second = await claim_batch(sessions, 10, LEASE_SECONDS)

    assert len(first) == 2
    assert second == []


async def test_expired_lease_is_reclaimed(sessions: SessionFactory) -> None:
    [notification_id] = await create_due_notifications(sessions, 1)
    await claim_batch(sessions, 10, LEASE_SECONDS)

    await expire_leases(sessions)
    [reclaimed] = await claim_batch(sessions, 10, LEASE_SECONDS)

    assert reclaimed.notification.notification_id == notification_id
    assert reclaimed.attempts == 2


async def test_stale_dispatcher_cannot_overwrite(sessions: SessionFactory) -> None:
    [notification_id] = await create_due_notifications(sessions, 1)
    await claim_batch(sessions, 10, LEASE_SECONDS)
    await expire_leases(sessions)
    await claim_batch(sessions, 10, LEASE_SECONDS)

    stale = Outcome(
        notification_id=notification_id,
        attempts=1,
        kind="sent",
        error=None,
        delay_seconds=None,
    )
    await record_outcomes(sessions, [stale])

    row = await stored(sessions, notification_id)
    assert row.status == "pending"
    assert row.sent_at is None
    assert row.attempts == 2

    # The dispatcher that holds the current lease can still record its result.
    await record_outcomes(
        sessions,
        [
            Outcome(
                notification_id=notification_id,
                attempts=2,
                kind="sent",
                error=None,
                delay_seconds=None,
            )
        ],
    )
    assert (await stored(sessions, notification_id)).status == "sent"


@pytest.mark.parametrize(
    ("final_kind", "final_error"),
    [("sent", None), ("failed", DeliveryFailure.NO_ADDRESS)],
)
async def test_final_row_is_not_overwritten_with_matching_attempts(
    sessions: SessionFactory,
    final_kind: Literal["sent", "failed"],
    final_error: DeliveryFailure | None,
) -> None:
    [notification_id] = await create_due_notifications(sessions, 1)
    await claim_batch(sessions, 10, LEASE_SECONDS)
    await record_outcomes(
        sessions,
        [
            Outcome(
                notification_id=notification_id,
                attempts=1,
                kind=final_kind,
                error=final_error,
                delay_seconds=None,
            )
        ],
    )
    before = await stored(sessions, notification_id)

    # Same `attempts`, so only the status clause of the fence can reject it.
    late = Outcome(
        notification_id=notification_id,
        attempts=1,
        kind="retry",
        error=DeliveryFailure.FAILED,
        delay_seconds=60,
    )
    await record_outcomes(sessions, [late])

    after = await stored(sessions, notification_id)
    assert after.status == final_kind
    assert after.last_error == (final_error and final_error.message)
    assert after.next_attempt_at == before.next_attempt_at


async def test_concurrent_dispatchers_send_once(
    sessions: SessionFactory, settings: Settings
) -> None:
    ids = await create_due_notifications(sessions, 50)
    sender = FakeSender()
    small_batches = settings.model_copy(update={"dispatch_batch_size": 7})

    await asyncio.gather(
        *(dispatch(sessions, registry(sender), small_batches) for _ in range(4))
    )

    assert Counter(sender.calls) == Counter(ids)
    for notification_id in ids:
        assert (await stored(sessions, notification_id)).status == "sent"

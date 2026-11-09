import asyncio
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from climate_alerts.agrobot.models import Field
from climate_alerts.alerts.models import Alert
from climate_alerts.config import Settings
from climate_alerts.db import SessionFactory
from climate_alerts.errors import DeliveryFailure
from climate_alerts.notifications.dispatch import (
    DispatchResult,
    Outcome,
    build_claim_statement,
    claim_batch,
    dispatch_due,
    record_outcomes,
)
from climate_alerts.notifications.models import Notification, NotificationStatus
from climate_alerts.notifications.senders import (
    NotificationSender,
    OutboundNotification,
    PermanentDeliveryError,
    build_sender_registry,
)
from tests.integration.factories import (
    create_alert,
    create_field,
    create_notification,
    create_user,
)

LEASE_SECONDS = 300


class FakeSender:
    """Records every call, then fails or hangs as configured."""

    def __init__(
        self,
        channel: str = "whatsapp",
        error: Exception | None = None,
        hang_ids: frozenset[int] = frozenset(),
    ) -> None:
        self.channel = channel
        self.error = error
        self.hang_ids = hang_ids
        self.calls: list[int] = []

    async def send(self, notification: OutboundNotification) -> None:
        self.calls.append(notification.notification_id)
        if notification.notification_id in self.hang_ids:
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error


class PeakTrackingSender:
    channel = "whatsapp"

    def __init__(self) -> None:
        self.current = self.peak = 0

    async def send(self, notification: OutboundNotification) -> None:
        self.current += 1
        self.peak = max(self.peak, self.current)
        await asyncio.sleep(0.01)
        self.current -= 1


def registry(*senders: NotificationSender) -> dict[str, NotificationSender]:
    return {sender.channel: sender for sender in senders}


async def create_owned_alert(session: AsyncSession, email: str | None = None) -> Alert:
    user = await create_user(session, email=email)
    field = await create_field(session, user, name="La Esperanza")
    return await create_alert(session, field)


async def create_due_notifications(
    sessions: SessionFactory, count: int, **overrides: Any
) -> list[int]:
    async with sessions() as session:
        alert = await create_owned_alert(session)
        created = [
            await create_notification(session, alert, days_ahead=day, **overrides)
            for day in range(count)
        ]
    return [notification.id for notification in created]


async def stored(sessions: SessionFactory, notification_id: int) -> Notification:
    async with sessions() as session:
        return await session.get_one(Notification, notification_id)


async def expire_leases(sessions: SessionFactory) -> None:
    async with sessions.begin() as session:
        await session.execute(
            update(Notification)
            .where(Notification.status == NotificationStatus.PENDING.value)
            .values(next_attempt_at=func.now() - timedelta(seconds=1))
        )


async def dispatch(
    sessions: SessionFactory,
    senders: Mapping[str, NotificationSender],
    settings: Settings,
) -> DispatchResult:
    return await dispatch_due(sessions, senders, settings, asyncio.Event())


async def test_success_marks_sent(sessions: SessionFactory, settings: Settings) -> None:
    [notification_id] = await create_due_notifications(sessions, 1)

    result = await dispatch(sessions, registry(FakeSender()), settings)

    row = await stored(sessions, notification_id)
    assert row.status == "sent"
    assert row.sent_at is not None
    assert row.last_error is None
    assert result == DispatchResult(claimed=1, sent=1, retried=0, failed=0)


async def test_permanent_error_marks_failed(
    sessions: SessionFactory, settings: Settings
) -> None:
    [notification_id] = await create_due_notifications(sessions, 1)
    sender = FakeSender(error=PermanentDeliveryError(DeliveryFailure.NO_ADDRESS))

    result = await dispatch(sessions, registry(sender), settings)

    row = await stored(sessions, notification_id)
    assert row.status == "failed"
    assert row.last_error == DeliveryFailure.NO_ADDRESS.message
    assert sender.calls == [notification_id]
    assert result == DispatchResult(claimed=1, sent=0, retried=0, failed=1)


async def test_transient_error_schedules_retry(
    sessions: SessionFactory, settings: Settings
) -> None:
    [notification_id] = await create_due_notifications(sessions, 1)
    sender = FakeSender(error=RuntimeError("provider said something private"))

    result = await dispatch(sessions, registry(sender), settings)

    row = await stored(sessions, notification_id)
    assert row.status == "pending"
    assert row.last_error == DeliveryFailure.FAILED.message
    async with sessions() as session:
        assert await session.scalar(select(func.now() < Notification.next_attempt_at))
    assert result == DispatchResult(claimed=1, sent=0, retried=1, failed=0)


class LeakyProviderError(Exception):
    """A class name that must never reach the client."""


async def test_unexpected_error_reaches_the_client_only_as_delivery_failed(
    sessions: SessionFactory,
    settings: Settings,
    client: AsyncClient,
    auth_headers: Callable[[int], dict[str, str]],
) -> None:
    secret = "+5491100000000 token=hunter2"
    async with sessions() as session:
        alert = await create_owned_alert(session)
        owner_id = (await session.get_one(Field, alert.field_id)).owner_id
        notification = await create_notification(session, alert)
    sender = FakeSender(error=LeakyProviderError(secret))

    with capture_logs() as logs:
        await dispatch(sessions, registry(sender), settings)

    row = await stored(sessions, notification.id)
    assert row.last_error == DeliveryFailure.FAILED.message
    response = await client.get("/api/v1/notifications", headers=auth_headers(owner_id))
    [item] = response.json()["items"]
    assert item["last_error"] == "Delivery failed"
    for leaked in ("hunter2", "+5491100000000", "LeakyProviderError"):
        assert leaked not in response.text
    failures = [log for log in logs if log["event"] == "dispatch.send_failed"]
    assert failures == [
        {
            "event": "dispatch.send_failed",
            "notification_id": notification.id,
            "channel": "whatsapp",
            "reason": "delivery_failed",
            "error_type": "LeakyProviderError",
            "log_level": "warning",
        }
    ]
    assert "hunter2" not in repr(logs)


async def seconds_until_next_attempt(
    sessions: SessionFactory, notification_id: int
) -> float:
    async with sessions() as session:
        remaining = await session.scalar(
            select(Notification.next_attempt_at - func.now()).where(
                Notification.id == notification_id
            )
        )
    assert remaining is not None
    return remaining.total_seconds()


@pytest.mark.parametrize(
    ("previous_attempts", "shortest", "longest"),
    [
        pytest.param(0, 30, 60, id="first_attempt"),
        pytest.param(2, 120, 240, id="third_attempt"),
    ],
)
async def test_retry_stores_the_backoff_not_the_lease(
    sessions: SessionFactory,
    settings: Settings,
    previous_attempts: int,
    shortest: float,
    longest: float,
) -> None:
    [notification_id] = await create_due_notifications(
        sessions, 1, attempts=previous_attempts
    )
    sender = FakeSender(error=RuntimeError("down"))

    await dispatch(sessions, registry(sender), settings)

    remaining = await seconds_until_next_attempt(sessions, notification_id)
    # The lower bound has slack for the time between recording and reading.
    assert shortest - 5 <= remaining <= longest
    assert remaining < LEASE_SECONDS


async def test_soft_deleted_alert_is_still_delivered(
    sessions: SessionFactory, settings: Settings
) -> None:
    [notification_id] = await create_due_notifications(sessions, 1)
    async with sessions.begin() as session:
        await session.execute(update(Alert).values(deleted_at=func.now()))
    sender = FakeSender()

    await dispatch(sessions, registry(sender), settings)

    assert (await stored(sessions, notification_id)).status == "sent"
    assert sender.calls == [notification_id]


async def test_last_attempt_marks_failed(
    sessions: SessionFactory, settings: Settings
) -> None:
    # The claim raises attempts to the maximum of 8.
    [notification_id] = await create_due_notifications(sessions, 1, attempts=7)
    sender = FakeSender(error=RuntimeError("down"))

    await dispatch(sessions, registry(sender), settings)

    row = await stored(sessions, notification_id)
    assert row.attempts == 8
    assert row.status == "failed"
    assert row.last_error is not None
    assert sender.calls == [notification_id]


async def test_expired_leases_exhaust_attempts(
    sessions: SessionFactory, settings: Settings
) -> None:
    # The claim raises attempts to 9, above the maximum of 8.
    [notification_id] = await create_due_notifications(sessions, 1, attempts=8)
    sender = FakeSender()

    result = await dispatch(sessions, registry(sender), settings)

    row = await stored(sessions, notification_id)
    assert row.attempts == 9
    assert row.status == "failed"
    assert row.last_error == DeliveryFailure.ATTEMPTS_EXHAUSTED.message
    assert sender.calls == []
    assert result.failed == 1


async def test_hanging_sender_times_out(
    sessions: SessionFactory, settings: Settings
) -> None:
    hanging, *others = await create_due_notifications(sessions, 3)
    sender = FakeSender(hang_ids=frozenset({hanging}))
    fast = settings.model_copy(update={"dispatch_send_timeout_seconds": 0.05})

    result = await dispatch(sessions, registry(sender), fast)

    row = await stored(sessions, hanging)
    assert row.status == "pending"
    assert row.last_error == DeliveryFailure.TIMED_OUT.message
    for other in others:
        assert (await stored(sessions, other)).status == "sent"
    assert result == DispatchResult(claimed=3, sent=2, retried=1, failed=0)


async def test_row_for_channel_without_sender_is_retried(
    sessions: SessionFactory, settings: Settings
) -> None:
    [notification_id] = await create_due_notifications(sessions, 1, channel="sms")

    result = await dispatch(sessions, registry(FakeSender()), settings)

    row = await stored(sessions, notification_id)
    assert row.status == "pending"
    assert row.last_error == DeliveryFailure.CHANNEL_UNAVAILABLE.message
    assert 25 <= await seconds_until_next_attempt(sessions, notification_id) <= 60
    assert result == DispatchResult(claimed=1, sent=0, retried=1, failed=0)


async def test_row_for_channel_without_sender_fails_at_the_maximum(
    sessions: SessionFactory, settings: Settings
) -> None:
    # The claim raises attempts to the maximum of 8.
    [notification_id] = await create_due_notifications(
        sessions, 1, channel="sms", attempts=7
    )

    result = await dispatch(sessions, registry(FakeSender()), settings)

    row = await stored(sessions, notification_id)
    assert row.status == "failed"
    assert row.last_error == DeliveryFailure.CHANNEL_UNAVAILABLE.message
    assert result == DispatchResult(claimed=1, sent=0, retried=0, failed=1)


async def test_missing_address_marks_failed(
    sessions: SessionFactory, settings: Settings
) -> None:
    [notification_id] = await create_due_notifications(sessions, 1, channel="email")

    await dispatch(sessions, build_sender_registry(["email"]), settings)

    row = await stored(sessions, notification_id)
    assert row.status == "failed"
    assert row.last_error == DeliveryFailure.NO_ADDRESS.message


async def test_channels_fail_independently(
    sessions: SessionFactory, settings: Settings
) -> None:
    async with sessions() as session:
        alert = await create_owned_alert(session, email="maria@example.com")
        whatsapp = await create_notification(session, alert, channel="whatsapp")
        email = await create_notification(session, alert, channel="email")
    whatsapp_sender = FakeSender("whatsapp")
    email_sender = FakeSender("email", error=RuntimeError("smtp down"))
    senders = registry(whatsapp_sender, email_sender)

    await dispatch(sessions, senders, settings)

    assert (await stored(sessions, whatsapp.id)).status == "sent"
    assert (await stored(sessions, email.id)).status == "pending"

    await expire_leases(sessions)
    email_sender.error = None
    await dispatch(sessions, senders, settings)

    assert whatsapp_sender.calls == [whatsapp.id]
    assert email_sender.calls == [email.id, email.id]
    row = await stored(sessions, email.id)
    assert row.status == "sent"
    assert row.last_error is None


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


async def explain_claim(sessions: SessionFactory, batch_size: int) -> str:
    statement = build_claim_statement(batch_size, LEASE_SECONDS)
    compiled = statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    async with sessions() as session:
        connection = await session.connection()
        result = await connection.exec_driver_sql(f"EXPLAIN {compiled}")
        return "\n".join(row[0] for row in result)


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

    stale = Outcome(notification_id, 1, "sent", None, None)
    await record_outcomes(sessions, [stale])

    row = await stored(sessions, notification_id)
    assert row.status == "pending"
    assert row.sent_at is None
    assert row.attempts == 2

    # The dispatcher that holds the current lease can still record its result.
    await record_outcomes(sessions, [Outcome(notification_id, 2, "sent", None, None)])
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
        sessions, [Outcome(notification_id, 1, final_kind, final_error, None)]
    )
    before = await stored(sessions, notification_id)

    # Same `attempts`, so only the status clause of the fence can reject it.
    late = Outcome(notification_id, 1, "retry", DeliveryFailure.FAILED, 60)
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


async def test_concurrency_is_bounded(
    sessions: SessionFactory, settings: Settings
) -> None:
    await create_due_notifications(sessions, 12)
    sender = PeakTrackingSender()
    limited = settings.model_copy(update={"dispatch_concurrency": 3})

    await dispatch(sessions, registry(sender), limited)

    assert sender.peak == limited.dispatch_concurrency


async def test_runs_batches_until_nothing_is_due(
    sessions: SessionFactory, settings: Settings
) -> None:
    await create_due_notifications(sessions, 5)
    sender = FakeSender()
    small_batches = settings.model_copy(update={"dispatch_batch_size": 2})

    result = await dispatch(sessions, registry(sender), small_batches)

    assert result == DispatchResult(claimed=5, sent=5, retried=0, failed=0)


async def test_stopping_prevents_the_next_batch(
    sessions: SessionFactory, settings: Settings
) -> None:
    await create_due_notifications(sessions, 3)
    stopping = asyncio.Event()

    class StopsAfterFirstSend(FakeSender):
        async def send(self, notification: OutboundNotification) -> None:
            await super().send(notification)
            stopping.set()

    sender = StopsAfterFirstSend()
    one_at_a_time = settings.model_copy(update={"dispatch_batch_size": 1})

    result = await dispatch_due(sessions, registry(sender), one_at_a_time, stopping)

    assert result == DispatchResult(claimed=1, sent=1, retried=0, failed=0)


async def test_logs_completion_without_addresses(
    sessions: SessionFactory, settings: Settings
) -> None:
    async with sessions() as session:
        alert = await create_owned_alert(session, email="maria@example.com")
        await create_notification(session, alert, channel="email")

    with capture_logs() as logs:
        await dispatch(sessions, build_sender_registry(["email"]), settings)

    [completed] = [log for log in logs if log["event"] == "dispatch.completed"]
    assert completed["claimed"] == 1
    assert completed["sent"] == 1
    assert completed["retried"] == 0
    assert completed["failed"] == 0
    assert completed["duration_ms"] >= 0
    assert "maria@example.com" not in str(logs)

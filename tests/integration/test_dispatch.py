import asyncio
from collections.abc import Callable

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from structlog.testing import capture_logs

from climate_alerts.agrobot.models import Field
from climate_alerts.alerts.models import Alert
from climate_alerts.config import Settings
from climate_alerts.db import SessionFactory
from climate_alerts.errors import DeliveryFailure
from climate_alerts.notifications.dispatch import (
    DispatchResult,
    dispatch_due,
)
from climate_alerts.notifications.models import Notification
from climate_alerts.notifications.senders import (
    OutboundNotification,
    PermanentDeliveryError,
    build_sender_registry,
)
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


class PeakTrackingSender:
    channel = "whatsapp"

    def __init__(self) -> None:
        self.current = self.peak = 0

    async def send(self, notification: OutboundNotification) -> None:
        self.current += 1
        self.peak = max(self.peak, self.current)
        await asyncio.sleep(0.01)
        self.current -= 1


class LeakyProviderError(Exception):
    """A class name that must never reach the client."""


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

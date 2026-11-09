import asyncio
import random
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

import structlog
from sqlalchemy import any_, func, select, update
from sqlalchemy.orm import aliased
from sqlalchemy.sql.dml import ReturningUpdate

from climate_alerts.agrobot.models import Field, User
from climate_alerts.alerts.models import Alert
from climate_alerts.config import Settings
from climate_alerts.db import SessionFactory
from climate_alerts.notifications.models import Notification, NotificationStatus
from climate_alerts.notifications.senders import (
    NotificationSender,
    OutboundNotification,
    PermanentDeliveryError,
    Recipient,
)

MAX_ERROR_LENGTH = 500

logger = structlog.get_logger()


@dataclass(frozen=True)
class Claimed:
    notification: OutboundNotification
    attempts: int


@dataclass(frozen=True)
class Outcome:
    """`attempts` is the fencing token: the value the claim left on the row."""

    notification_id: int
    attempts: int
    kind: Literal["sent", "retry", "failed"]
    error: str | None
    delay_seconds: float | None


@dataclass(frozen=True)
class DispatchResult:
    claimed: int
    sent: int
    retried: int
    failed: int


def backoff_seconds(
    attempts: int, base: float, cap: float, rng: random.Random
) -> float:
    """Exponential delay, capped, with jitter so retries do not arrive together."""
    return float(min(cap, base * 2 ** (attempts - 1))) * rng.uniform(0.5, 1.0)


def build_claim_statement(
    batch_size: int, lease_seconds: float
) -> ReturningUpdate[*tuple[Any, ...]]:
    """Take a lease on the oldest due rows and return what sending them needs.

    Moving `next_attempt_at` forward is the lease: the row stops being due, so
    no transaction has to stay open while sending. If the dispatcher dies, the
    row becomes due again when the lease expires. SKIP LOCKED makes concurrent
    dispatchers take different rows instead of waiting on each other.

    The ids are picked inside `ARRAY(...)`, which PostgreSQL evaluates once as
    an InitPlan, then updated through the primary key. Inside `WHERE id IN
    (...)` the planner may re-run the subquery per outer row when statistics
    are stale, and each re-run skips the rows this command already updated, so
    LIMIT stops bounding the batch; with healthy statistics it hashes the
    subquery and scans the whole table instead.

    The recipient comes from `UPDATE ... FROM`, which locks only the updated
    rows. The joins are many-to-one through RESTRICT foreign keys, so every
    claimed row is returned exactly once.
    """
    # The alias is required: with `Notification` itself SQLAlchemy correlates
    # the subquery to the UPDATE target and drops the subquery's FROM clause.
    due = aliased(Notification)
    due_ids = (
        select(due.id)
        .where(
            due.status == NotificationStatus.PENDING.value,
            due.next_attempt_at <= func.now(),
        )
        .order_by(due.next_attempt_at, due.id)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    return (
        update(Notification)
        .where(
            Notification.id == any_(func.array(due_ids.scalar_subquery())),
            Alert.id == Notification.alert_id,
            Field.id == Alert.field_id,
            User.id == Field.owner_id,
        )
        .values(
            attempts=Notification.attempts + 1,
            next_attempt_at=func.now() + timedelta(seconds=lease_seconds),
        )
        .returning(
            Notification.id,
            Notification.forecast_date,
            Notification.channel,
            Notification.probability,
            Notification.threshold,
            Notification.attempts,
            Alert.event_type,
            Field.name.label("field_name"),
            User.phone,
            User.email,
        )
    )


async def claim_batch(
    sessions: SessionFactory, batch_size: int, lease_seconds: float
) -> list[Claimed]:
    statement = build_claim_statement(batch_size, lease_seconds)
    async with sessions.begin() as session:
        rows = (await session.execute(statement)).all()
    return [
        Claimed(
            OutboundNotification(
                notification_id=row.id,
                channel=row.channel,
                recipient=Recipient(phone=row.phone, email=row.email),
                field_name=row.field_name,
                event_type=row.event_type,
                forecast_date=row.forecast_date,
                probability=row.probability,
                threshold=row.threshold,
            ),
            row.attempts,
        )
        for row in rows
    ]


async def deliver(
    claimed: Claimed,
    senders: Mapping[str, NotificationSender],
    settings: Settings,
    rng: random.Random,
) -> Outcome:
    """Send one notification and classify the result. Never raises an `Exception`.

    `last_error` must hold only messages we control, so an unexpected error is
    reduced to its type: its text could carry provider details or an address.
    """
    notification = claimed.notification

    def failed(reason: str) -> Outcome:
        return Outcome(
            notification.notification_id, claimed.attempts, "failed", reason, None
        )

    def retry_or_fail(reason: str) -> Outcome:
        if claimed.attempts >= settings.dispatch_max_attempts:
            return failed(reason)
        delay = backoff_seconds(
            claimed.attempts,
            settings.dispatch_backoff_base_seconds,
            settings.dispatch_backoff_cap_seconds,
            rng,
        )
        return Outcome(
            notification.notification_id, claimed.attempts, "retry", reason, delay
        )

    # Attempts count claims, so a dispatcher that died mid-send used one up.
    if claimed.attempts > settings.dispatch_max_attempts:
        return failed("attempts exhausted: earlier leases expired without a result")

    sender = senders.get(notification.channel)
    if sender is None:
        # Transient on purpose: a replica mid-rollout may not know a channel
        # yet, and that must not permanently fail a deliverable notification.
        return retry_or_fail(f"no sender registered for channel {notification.channel}")

    try:
        async with asyncio.timeout(settings.dispatch_send_timeout_seconds):
            await sender.send(notification)
    except PermanentDeliveryError as exc:
        return failed(str(exc))
    except TimeoutError:
        return retry_or_fail("send timed out")
    except Exception as exc:
        return retry_or_fail(f"send failed: {type(exc).__name__}")
    return Outcome(notification.notification_id, claimed.attempts, "sent", None, None)


def _outcome_values(outcome: Outcome) -> dict[str, Any]:
    if outcome.kind == "sent":
        return {
            "status": NotificationStatus.SENT.value,
            "sent_at": func.now(),
            "last_error": None,
        }
    error = (outcome.error or "")[:MAX_ERROR_LENGTH]
    if outcome.kind == "failed":
        return {"status": NotificationStatus.FAILED.value, "last_error": error}
    delay = timedelta(seconds=outcome.delay_seconds or 0)
    return {"next_attempt_at": func.now() + delay, "last_error": error}


async def record_outcomes(
    sessions: SessionFactory, outcomes: Sequence[Outcome]
) -> None:
    """Store every outcome in one short transaction.

    The `attempts` filter is the fence: once a lease expires and another
    dispatcher claims the row, `attempts` has moved on and this update matches
    nothing, so the dispatcher that lost the lease cannot overwrite the result.

    The `status` filter covers what `attempts` cannot: a row that already
    reached `sent` or `failed` keeps the same `attempts`, so without it a late
    or repeated outcome with a matching value could overwrite the final state.
    """
    async with sessions.begin() as session:
        for outcome in outcomes:
            await session.execute(
                update(Notification)
                .where(
                    Notification.id == outcome.notification_id,
                    Notification.attempts == outcome.attempts,
                    Notification.status == NotificationStatus.PENDING.value,
                )
                .values(_outcome_values(outcome))
            )


async def dispatch_due(
    sessions: SessionFactory,
    senders: Mapping[str, NotificationSender],
    settings: Settings,
    stopping: asyncio.Event,
    rng: random.Random | None = None,
) -> DispatchResult:
    """Claim, send and record batches until nothing is due or `stopping` is set.

    `stopping` is checked between batches, so a batch that has started is
    finished and recorded. No transaction is open while sending.
    """
    started = time.perf_counter()
    jitter = rng if rng is not None else random.Random()  # noqa: S311 - not security
    semaphore = asyncio.Semaphore(settings.dispatch_concurrency)
    claimed_count = sent = retried = failed = 0

    async def deliver_bounded(claimed: Claimed) -> Outcome:
        async with semaphore:
            return await deliver(claimed, senders, settings, jitter)

    while not stopping.is_set():
        batch = await claim_batch(
            sessions, settings.dispatch_batch_size, settings.dispatch_lease_seconds
        )
        if not batch:
            break

        outcomes = await asyncio.gather(*map(deliver_bounded, batch))
        await record_outcomes(sessions, outcomes)

        claimed_count += len(batch)
        sent += sum(outcome.kind == "sent" for outcome in outcomes)
        retried += sum(outcome.kind == "retry" for outcome in outcomes)
        failed += sum(outcome.kind == "failed" for outcome in outcomes)

    logger.info(
        "dispatch.completed",
        claimed=claimed_count,
        sent=sent,
        retried=retried,
        failed=failed,
        duration_ms=round((time.perf_counter() - started) * 1000),
    )
    return DispatchResult(claimed_count, sent, retried, failed)

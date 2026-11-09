from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

import structlog

logger = structlog.get_logger()


@dataclass(frozen=True)
class Recipient:
    phone: str
    email: str | None


@dataclass(frozen=True)
class OutboundNotification:
    notification_id: int  # idempotency key for real providers
    channel: str
    recipient: Recipient
    field_name: str
    event_type: str
    forecast_date: date
    probability: Decimal
    threshold: Decimal


class NotificationSender(Protocol):
    channel: str

    async def send(self, notification: OutboundNotification) -> None: ...


class PermanentDeliveryError(Exception):
    """Delivery can never succeed, so retrying is pointless.

    The message is stored in `last_error`: it must not carry provider text,
    phone numbers or emails.
    """


class SimulatedWhatsAppSender:
    channel = "whatsapp"

    async def send(self, notification: OutboundNotification) -> None:
        _log_simulated_send(notification, _mask_phone(notification.recipient.phone))


class SimulatedEmailSender:
    channel = "email"

    async def send(self, notification: OutboundNotification) -> None:
        email = notification.recipient.email
        if not email:
            raise PermanentDeliveryError("recipient has no email address")
        _log_simulated_send(notification, _mask_email(email))


_SENDERS: dict[str, type[NotificationSender]] = {
    sender.channel: sender for sender in (SimulatedWhatsAppSender, SimulatedEmailSender)
}


def build_sender_registry(channels: Sequence[str]) -> dict[str, NotificationSender]:
    """Build one sender per channel. Raises `ValueError` for an unknown channel."""
    unknown = [channel for channel in channels if channel not in _SENDERS]
    if unknown:
        raise ValueError(f"no sender for channels: {', '.join(unknown)}")
    return {channel: _SENDERS[channel]() for channel in channels}


def _mask_phone(phone: str) -> str:
    return f"***{phone[-4:]}"


def _mask_email(email: str) -> str:
    return f"{email[0]}***@***"


def _log_simulated_send(notification: OutboundNotification, masked_to: str) -> None:
    logger.info(
        "notification.simulated_send",
        notification_id=notification.notification_id,
        channel=notification.channel,
        to=masked_to,
        field_name=notification.field_name,
        event_type=notification.event_type,
        forecast_date=notification.forecast_date.isoformat(),
        probability=str(notification.probability),
        threshold=str(notification.threshold),
    )

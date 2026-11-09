from datetime import date
from decimal import Decimal

import pytest
from structlog.testing import capture_logs

from climate_alerts.notifications.senders import (
    OutboundNotification,
    PermanentDeliveryError,
    Recipient,
    SimulatedEmailSender,
    SimulatedWhatsAppSender,
    build_sender_registry,
)

PHONE = "+5491155551234"
EMAIL = "maria.perez@example.com"


def outbound(channel: str, email: str | None) -> OutboundNotification:
    return OutboundNotification(
        notification_id=1,
        channel=channel,
        recipient=Recipient(phone=PHONE, email=email),
        field_name="La Esperanza",
        event_type="frost",
        forecast_date=date(2026, 7, 1),
        probability=Decimal("90.00"),
        threshold=Decimal("70.00"),
    )


def test_registry_builds_a_sender_per_channel() -> None:
    registry = build_sender_registry(["whatsapp", "email"])

    assert {name: sender.channel for name, sender in registry.items()} == {
        "whatsapp": "whatsapp",
        "email": "email",
    }


def test_registry_rejects_unknown_channel() -> None:
    with pytest.raises(ValueError, match="sms"):
        build_sender_registry(["sms"])


async def test_email_without_address_is_permanent() -> None:
    with pytest.raises(PermanentDeliveryError):
        await SimulatedEmailSender().send(outbound("email", None))


async def test_simulated_sender_masks_recipient() -> None:
    with capture_logs() as logs:
        await SimulatedWhatsAppSender().send(outbound("whatsapp", EMAIL))
        await SimulatedEmailSender().send(outbound("email", EMAIL))

    assert len(logs) == 2
    captured = str(logs)
    assert PHONE not in captured
    assert EMAIL not in captured
    assert "maria.perez" not in captured
    assert "example.com" not in captured

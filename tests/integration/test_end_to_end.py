"""The whole pipeline once, through the same code the worker and the API run."""

import asyncio
from collections.abc import Callable

from httpx import AsyncClient

from climate_alerts.agrobot.seed import seed
from climate_alerts.config import Settings
from climate_alerts.db import SessionFactory
from climate_alerts.notifications.dispatch import dispatch_due
from climate_alerts.notifications.evaluation import evaluate_alerts
from climate_alerts.notifications.senders import build_sender_registry

URL = "/api/v1/notifications"


async def test_seeded_forecast_reaches_the_notification_history(
    client: AsyncClient,
    sessions: SessionFactory,
    settings: Settings,
    auth_headers: Callable[[int], dict[str, str]],
) -> None:
    configured = settings.model_copy(update={"notification_channels": "whatsapp,email"})
    seeded = await seed(sessions, configured)
    never_stop = asyncio.Event()
    senders = build_sender_registry(configured.enabled_channels)

    evaluated = await evaluate_alerts(sessions, configured, never_stop)
    dispatched = await dispatch_due(sessions, senders, configured, never_stop)

    assert evaluated.created == 4
    assert (dispatched.sent, dispatched.failed) == (3, 1)

    with_email, without_email, _ = seeded
    own = await client.get(URL, headers=auth_headers(with_email.id))
    other = await client.get(URL, headers=auth_headers(without_email.id))

    assert own.status_code == 200
    assert {
        (item["channel"], item["status"], item["event_type"], item["probability"])
        for item in own.json()["items"]
    } == {
        ("whatsapp", "sent", "frost", "85.00"),
        ("email", "sent", "frost", "85.00"),
    }
    assert all(item["sent_at"] is not None for item in own.json()["items"])
    assert {
        (item["channel"], item["status"], item["last_error"])
        for item in other.json()["items"]
    } == {
        ("whatsapp", "sent", None),
        ("email", "failed", "recipient has no email address"),
    }

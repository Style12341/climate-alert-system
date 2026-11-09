from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient

from climate_alerts.agrobot.models import Field, User
from climate_alerts.alerts.models import Alert
from climate_alerts.db import SessionFactory
from climate_alerts.errors import DeliveryFailure
from climate_alerts.notifications.models import Notification
from climate_alerts.notifications.router import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from tests.integration.factories import (
    create_alert,
    create_field,
    create_notification,
    create_user,
)

AuthHeaders = Callable[[int], dict[str, str]]

URL = "/api/v1/notifications"


async def owner_with_alert(sessions: SessionFactory) -> tuple[User, Field, Alert]:
    async with sessions() as session:
        user = await create_user(session)
        field = await create_field(session, user)
        alert = await create_alert(session, field)
    return user, field, alert


async def add_notifications(
    sessions: SessionFactory, alert: Alert, count: int
) -> list[Notification]:
    async with sessions() as session:
        return [
            await create_notification(session, alert, days_ahead=day)
            for day in range(count)
        ]


async def test_lists_only_own_notifications(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    user, _, alert = await owner_with_alert(sessions)
    _, _, foreign_alert = await owner_with_alert(sessions)
    [own] = await add_notifications(sessions, alert, 1)
    await add_notifications(sessions, foreign_alert, 1)

    response = await client.get(URL, headers=auth_headers(user.id))

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [own.id]


async def test_body_has_spec_fields(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    user, field, alert = await owner_with_alert(sessions)
    [notification] = await add_notifications(sessions, alert, 1)

    response = await client.get(URL, headers=auth_headers(user.id))

    body = response.json()
    assert set(body) == {"items", "next_cursor"}
    [item] = body["items"]
    assert set(item) == {
        "id",
        "alert_id",
        "field_id",
        "event_type",
        "forecast_date",
        "channel",
        "probability",
        "threshold",
        "status",
        "attempts",
        "last_error",
        "created_at",
        "sent_at",
    }
    assert item["id"] == notification.id
    assert item["alert_id"] == alert.id
    assert item["field_id"] == field.id
    assert item["event_type"] == alert.event_type


@pytest.mark.parametrize(
    ("param", "value", "expected_index"),
    [
        ("status", "sent", 1),
        ("channel", "email", 1),
        ("field_id", None, 2),
    ],
    ids=["status", "channel", "field_id"],
)
async def test_filters(
    client: AsyncClient,
    sessions: SessionFactory,
    auth_headers: AuthHeaders,
    param: str,
    value: str | None,
    expected_index: int,
) -> None:
    user, _, alert = await owner_with_alert(sessions)
    async with sessions() as session:
        other_field = await create_field(session, user, name="Other field")
        other_alert = await create_alert(session, other_field)
        rows = [
            await create_notification(session, alert, days_ahead=0),
            await create_notification(
                session,
                alert,
                days_ahead=1,
                channel="email",
                status="sent",
                sent_at=datetime.now(UTC),
            ),
            await create_notification(session, other_alert, days_ahead=2),
        ]
    params: dict[str, Any] = {param: value if value is not None else other_field.id}

    response = await client.get(URL, params=params, headers=auth_headers(user.id))

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [
        rows[expected_index].id
    ]


async def test_field_filter_cannot_reach_another_users_field(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    user, _, _ = await owner_with_alert(sessions)
    _, foreign_field, foreign_alert = await owner_with_alert(sessions)
    await add_notifications(sessions, foreign_alert, 1)

    response = await client.get(
        URL, params={"field_id": foreign_field.id}, headers=auth_headers(user.id)
    )

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


async def test_pagination(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    user, _, alert = await owner_with_alert(sessions)
    oldest, middle, newest = await add_notifications(sessions, alert, 3)
    headers = auth_headers(user.id)

    first = (await client.get(URL, params={"limit": 2}, headers=headers)).json()

    assert [item["id"] for item in first["items"]] == [newest.id, middle.id]
    assert first["next_cursor"] == middle.id

    second = (
        await client.get(
            URL, params={"limit": 2, "cursor": first["next_cursor"]}, headers=headers
        )
    ).json()

    assert [item["id"] for item in second["items"]] == [oldest.id]
    assert second["next_cursor"] is None


async def test_exactly_full_page_has_no_next_cursor(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    user, _, alert = await owner_with_alert(sessions)
    await add_notifications(sessions, alert, 2)

    response = await client.get(URL, params={"limit": 2}, headers=auth_headers(user.id))

    body = response.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is None


async def test_default_page_size(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    user, _, alert = await owner_with_alert(sessions)
    await add_notifications(sessions, alert, DEFAULT_PAGE_SIZE + 1)

    response = await client.get(URL, headers=auth_headers(user.id))

    body = response.json()
    assert len(body["items"]) == DEFAULT_PAGE_SIZE
    assert body["next_cursor"] is not None


async def test_limit_at_maximum_is_accepted(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    user, _, _ = await owner_with_alert(sessions)

    response = await client.get(
        URL, params={"limit": MAX_PAGE_SIZE}, headers=auth_headers(user.id)
    )

    assert response.status_code == 200


async def test_limit_above_maximum_returns_422(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    user, _, _ = await owner_with_alert(sessions)

    response = await client.get(
        URL, params={"limit": MAX_PAGE_SIZE + 1}, headers=auth_headers(user.id)
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": [
            {
                "type": "less_than_equal",
                "loc": ["query", "limit"],
                "msg": f"Input should be less than or equal to {MAX_PAGE_SIZE}",
            }
        ]
    }


@pytest.mark.parametrize(
    ("param", "value"),
    [
        ("limit", "0"),
        ("limit", "-1"),
        ("limit", "99999999999999999999"),
        ("cursor", "0"),
        ("cursor", "-1"),
        ("cursor", "99999999999999999999"),
        ("field_id", "0"),
        ("field_id", "-1"),
        ("field_id", "99999999999999999999"),
        ("status", "unknown"),
        ("status", "sent\x00"),
        ("channel", "whatsapp\x00"),
    ],
)
async def test_invalid_query_returns_422(
    client: AsyncClient,
    sessions: SessionFactory,
    auth_headers: AuthHeaders,
    param: str,
    value: str,
) -> None:
    user, _, _ = await owner_with_alert(sessions)

    response = await client.get(
        URL, params={param: value}, headers=auth_headers(user.id)
    )

    assert response.status_code == 422


async def test_deleted_alert_keeps_history(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    user, _, alert = await owner_with_alert(sessions)
    [notification] = await add_notifications(sessions, alert, 1)
    headers = auth_headers(user.id)
    deleted = await client.delete(f"/api/v1/alerts/{alert.id}", headers=headers)
    assert deleted.status_code == 204

    response = await client.get(URL, headers=headers)

    assert [item["id"] for item in response.json()["items"]] == [notification.id]


async def test_openapi_lists_the_delivery_failure_reasons(client: AsyncClient) -> None:
    schemas = (await client.get("/openapi.json")).json()["components"]["schemas"]

    description = schemas["NotificationOut"]["properties"]["last_error"]["description"]
    assert all(failure.message in description for failure in DeliveryFailure)


@pytest.mark.parametrize(
    "legacy_text",
    [
        "send failed: RuntimeError",
        "no sender registered for channel sms",
    ],
)
async def test_stored_text_that_is_not_a_delivery_failure_is_hidden(
    client: AsyncClient,
    sessions: SessionFactory,
    auth_headers: AuthHeaders,
    legacy_text: str,
) -> None:
    """Rows written before the client-safe reasons existed may hold old text."""
    user, _, alert = await owner_with_alert(sessions)
    async with sessions() as session:
        await create_notification(
            session, alert, status="failed", attempts=1, last_error=legacy_text
        )

    response = await client.get(URL, headers=auth_headers(user.id))

    assert response.status_code == 200
    [item] = response.json()["items"]
    assert item["last_error"] == DeliveryFailure.FAILED.message
    assert "RuntimeError" not in response.text
    assert legacy_text not in response.text


async def test_stored_delivery_failure_message_is_returned_as_is(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    user, _, alert = await owner_with_alert(sessions)
    async with sessions() as session:
        await create_notification(
            session,
            alert,
            status="failed",
            attempts=1,
            last_error=DeliveryFailure.TIMED_OUT.message,
        )

    response = await client.get(URL, headers=auth_headers(user.id))

    [item] = response.json()["items"]
    assert item["last_error"] == DeliveryFailure.TIMED_OUT.message

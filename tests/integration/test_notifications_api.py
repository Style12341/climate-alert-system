from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from climate_alerts.agrobot.models import Field, User
from climate_alerts.alerts.models import Alert
from climate_alerts.api.app import create_app
from climate_alerts.config import Settings
from climate_alerts.db import SessionFactory
from climate_alerts.notifications.models import Notification
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


async def test_requires_a_token(client: AsyncClient) -> None:
    response = await client.get(URL)

    assert response.status_code == 401


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


async def test_limit_at_maximum_is_accepted(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    user, _, _ = await owner_with_alert(sessions)

    response = await client.get(
        URL, params={"limit": 200}, headers=auth_headers(user.id)
    )

    assert response.status_code == 200


async def test_limit_above_maximum_returns_422(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    user, _, _ = await owner_with_alert(sessions)

    response = await client.get(
        URL, params={"limit": 201}, headers=auth_headers(user.id)
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": [
            {
                "type": "value_error",
                "loc": ["query", "limit"],
                "msg": "limit must be at most 200",
            }
        ]
    }


async def test_page_size_comes_from_settings(
    settings: Settings, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    user, _, alert = await owner_with_alert(sessions)
    await add_notifications(sessions, alert, 3)
    configured = settings.model_copy(
        update={"default_page_size": 2, "max_page_size": 3}
    )
    headers = auth_headers(user.id)

    async with LifespanManager(create_app(configured)) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            default_page = await http.get(URL, headers=headers)
            over_maximum = await http.get(URL, params={"limit": 4}, headers=headers)

    assert len(default_page.json()["items"]) == 2
    assert over_maximum.status_code == 422


async def test_oversized_cursor_returns_422(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    user, _, _ = await owner_with_alert(sessions)

    response = await client.get(
        URL,
        params={"cursor": "99999999999999999999"},
        headers=auth_headers(user.id),
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("param", "value"),
    [
        ("limit", "0"),
        ("limit", "-1"),
        ("limit", "99999999999999999999"),
        ("cursor", "0"),
        ("cursor", "-1"),
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

import asyncio
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from climate_alerts.agrobot.models import Field, User
from climate_alerts.alerts.models import Alert
from climate_alerts.db import SessionFactory
from tests.integration.factories import create_alert, create_field, create_user

AuthHeaders = Callable[[int], dict[str, str]]


async def owner_with_field(sessions: SessionFactory) -> tuple[User, Field]:
    async with sessions() as session:
        user = await create_user(session)
        field = await create_field(session, user)
    return user, field


def alerts_url(field: Field) -> str:
    return f"/api/v1/fields/{field.id}/alerts"


async def test_create_alert(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    user, field = await owner_with_field(sessions)

    response = await client.post(
        alerts_url(field),
        json={"event_type": "frost", "threshold": "70.50"},
        headers=auth_headers(user.id),
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "id",
        "field_id",
        "event_type",
        "threshold",
        "created_at",
        "updated_at",
    }
    assert body["field_id"] == field.id
    assert body["event_type"] == "frost"
    assert Decimal(body["threshold"]) == Decimal("70.50")


@pytest.mark.parametrize(
    ("threshold", "expected"),
    [(100, 201), (0, 422), (100.01, 422), (70.123, 422)],
)
async def test_threshold_bounds(
    client: AsyncClient,
    sessions: SessionFactory,
    auth_headers: AuthHeaders,
    threshold: float,
    expected: int,
) -> None:
    user, field = await owner_with_field(sessions)

    response = await client.post(
        alerts_url(field),
        json={"event_type": "frost", "threshold": threshold},
        headers=auth_headers(user.id),
    )

    assert response.status_code == expected


async def test_unknown_event_returns_422(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    user, field = await owner_with_field(sessions)

    response = await client.post(
        alerts_url(field),
        json={"event_type": "tornado", "threshold": 70},
        headers=auth_headers(user.id),
    )

    assert response.status_code == 422


async def test_event_type_with_nul_byte_returns_422(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    user, field = await owner_with_field(sessions)

    response = await client.post(
        alerts_url(field),
        json={"event_type": "fr\u0000ost", "threshold": 70},
        headers=auth_headers(user.id),
    )

    assert response.status_code == 422


async def test_duplicate_returns_409(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    user, field = await owner_with_field(sessions)
    payload = {"event_type": "frost", "threshold": 70}
    headers = auth_headers(user.id)

    first = await client.post(alerts_url(field), json=payload, headers=headers)
    second = await client.post(alerts_url(field), json=payload, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 409


async def test_concurrent_creates_conflict_once(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    user, field = await owner_with_field(sessions)
    payload = {"event_type": "frost", "threshold": 70}
    headers = auth_headers(user.id)

    responses = await asyncio.gather(
        client.post(alerts_url(field), json=payload, headers=headers),
        client.post(alerts_url(field), json=payload, headers=headers),
    )

    assert sorted(r.status_code for r in responses) == [201, 409]


async def test_create_on_foreign_field_returns_404(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    _, foreign_field = await owner_with_field(sessions)
    intruder, _ = await owner_with_field(sessions)

    response = await client.post(
        alerts_url(foreign_field),
        json={"event_type": "frost", "threshold": 70},
        headers=auth_headers(intruder.id),
    )

    assert response.status_code == 404


async def test_list_scoped_and_filtered(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    user, field = await owner_with_field(sessions)
    _, other_field = await owner_with_field(sessions)
    async with sessions() as session:
        second_field = await create_field(session, user, name="Second field")
        kept = await create_alert(session, field, "frost")
        in_second = await create_alert(session, second_field, "hail")
        deleted = await create_alert(session, field, "rain")
        await create_alert(session, other_field, "frost")
    headers = auth_headers(user.id)
    removal = await client.delete(f"/api/v1/alerts/{deleted.id}", headers=headers)
    assert removal.status_code == 204

    everything = await client.get("/api/v1/alerts", headers=headers)
    narrowed = await client.get(
        "/api/v1/alerts", params={"field_id": field.id}, headers=headers
    )

    assert everything.status_code == 200
    assert {a["id"] for a in everything.json()} == {kept.id, in_second.id}
    assert [a["id"] for a in narrowed.json()] == [kept.id]


async def test_oversized_field_filter_returns_422(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    user, _ = await owner_with_field(sessions)

    response = await client.get(
        "/api/v1/alerts",
        params={"field_id": 99999999999999999999},
        headers=auth_headers(user.id),
    )

    assert response.status_code == 422


async def test_read_foreign_alert_returns_404(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    _, foreign_field = await owner_with_field(sessions)
    intruder, _ = await owner_with_field(sessions)
    async with sessions() as session:
        alert = await create_alert(session, foreign_field)

    response = await client.get(
        f"/api/v1/alerts/{alert.id}", headers=auth_headers(intruder.id)
    )

    assert response.status_code == 404


async def test_patch_changes_threshold_only(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    user, field = await owner_with_field(sessions)
    headers = auth_headers(user.id)
    created = (
        await client.post(
            alerts_url(field),
            json={"event_type": "frost", "threshold": 70},
            headers=headers,
        )
    ).json()
    url = f"/api/v1/alerts/{created['id']}"

    patched = await client.patch(url, json={"threshold": "85.25"}, headers=headers)
    with_event = await client.patch(
        url, json={"threshold": 90, "event_type": "hail"}, headers=headers
    )
    out_of_range = await client.patch(url, json={"threshold": 0}, headers=headers)

    assert patched.status_code == 200
    body = patched.json()
    assert Decimal(body["threshold"]) == Decimal("85.25")
    assert body["event_type"] == "frost"
    assert body["created_at"] == created["created_at"]
    assert datetime.fromisoformat(body["updated_at"]) > datetime.fromisoformat(
        created["updated_at"]
    )
    assert with_event.status_code == 422
    assert out_of_range.status_code == 422


async def test_patch_foreign_alert_returns_404(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    _, foreign_field = await owner_with_field(sessions)
    intruder, _ = await owner_with_field(sessions)
    async with sessions() as session:
        alert = await create_alert(session, foreign_field, threshold="70")

    response = await client.patch(
        f"/api/v1/alerts/{alert.id}",
        json={"threshold": 10},
        headers=auth_headers(intruder.id),
    )

    assert response.status_code == 404
    async with sessions() as session:
        stored = await session.get_one(Alert, alert.id)
    assert stored.threshold == Decimal("70")


async def test_delete_is_soft_and_not_repeatable(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    user, field = await owner_with_field(sessions)
    async with sessions() as session:
        alert = await create_alert(session, field)
    url = f"/api/v1/alerts/{alert.id}"
    headers = auth_headers(user.id)

    first = await client.delete(url, headers=headers)
    second = await client.delete(url, headers=headers)

    assert first.status_code == 204
    assert second.status_code == 404
    async with sessions() as session:
        deleted_at = await session.scalar(
            select(Alert.deleted_at).where(Alert.id == alert.id)
        )
    assert deleted_at is not None


async def test_delete_foreign_alert_returns_404(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    _, foreign_field = await owner_with_field(sessions)
    intruder, _ = await owner_with_field(sessions)
    async with sessions() as session:
        alert = await create_alert(session, foreign_field)

    response = await client.delete(
        f"/api/v1/alerts/{alert.id}", headers=auth_headers(intruder.id)
    )

    assert response.status_code == 404
    async with sessions() as session:
        stored = await session.get_one(Alert, alert.id)
    assert stored.deleted_at is None


async def test_recreate_after_delete(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    user, field = await owner_with_field(sessions)
    async with sessions() as session:
        alert = await create_alert(session, field)
    headers = auth_headers(user.id)
    await client.delete(f"/api/v1/alerts/{alert.id}", headers=headers)

    response = await client.post(
        alerts_url(field),
        json={"event_type": "frost", "threshold": 70},
        headers=headers,
    )

    assert response.status_code == 201
    assert response.json()["id"] != alert.id


async def test_oversized_path_id_returns_422(
    client: AsyncClient, sessions: SessionFactory, auth_headers: AuthHeaders
) -> None:
    user, _ = await owner_with_field(sessions)

    response = await client.get(
        "/api/v1/alerts/99999999999999999999", headers=auth_headers(user.id)
    )

    assert response.status_code == 422

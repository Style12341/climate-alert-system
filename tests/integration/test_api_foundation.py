from collections.abc import Callable

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr
from structlog.testing import capture_logs

from climate_alerts.api.app import create_app
from climate_alerts.config import Settings
from climate_alerts.db import build_engine, build_session_factory

CATALOG = "/api/v1/climate-event-types"

# Read from the app so that a route added later is covered without editing this.
BUSINESS_ROUTES = sorted(
    (method.upper(), path)
    for path, operations in create_app(
        Settings(
            _env_file=None,
            database_url="postgresql+asyncpg://unused",
            jwt_secret="x" * 32,
        )
    )
    .openapi()["paths"]
    .items()
    if path.startswith("/api/v1")
    for method in operations
)


async def test_no_token_returns_401(client: AsyncClient) -> None:
    response = await client.get(CATALOG)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(("method", "path"), BUSINESS_ROUTES)
async def test_business_route_requires_a_token(
    client: AsyncClient, method: str, path: str
) -> None:
    url = path.format(field_id=1, alert_id=1)

    response = await client.request(method, url)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_every_business_route_is_collected() -> None:
    assert len(BUSINESS_ROUTES) == 7


async def test_invalid_token_returns_401(client: AsyncClient) -> None:
    response = await client.get(CATALOG, headers={"Authorization": "Bearer abc"})

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_catalog_lists_events(
    client: AsyncClient, auth_headers: Callable[[int], dict[str, str]]
) -> None:
    response = await client.get(CATALOG, headers=auth_headers(1))

    assert response.status_code == 200
    items = response.json()
    assert len(items) == 7
    assert all(set(item) == {"code", "label"} for item in items)


async def test_live_needs_no_token(client: AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200


async def test_ready_ok(client: AsyncClient) -> None:
    response = await client.get("/health/ready")

    assert response.status_code == 200


async def test_ready_when_database_is_down(
    app: FastAPI, client: AsyncClient, settings: Settings
) -> None:
    # Port 1 is reserved and nothing listens on it.
    unreachable = settings.model_copy(
        update={
            "database_url": SecretStr("postgresql+asyncpg://user:pass@127.0.0.1:1/none")
        }
    )
    engine = build_engine(unreachable)
    app.state.sessions = build_session_factory(engine)

    try:
        with capture_logs() as logs:
            response = await client.get("/health/ready")
    finally:
        await engine.dispose()

    assert response.status_code == 503
    # Exact match: the driver's message can carry connection details, so only
    # the exception type may be logged.
    assert logs == [
        {
            "event": "health.ready_failed",
            "error_type": "ConnectionRefusedError",
            "log_level": "warning",
        }
    ]


async def test_openapi_declares_the_error_contract(client: AsyncClient) -> None:
    document = (await client.get("/openapi.json")).json()
    paths = document["paths"]

    assert document["components"]["schemas"]["ErrorOut"]["properties"] == {
        "detail": {"title": "Detail", "type": "string"}
    }
    for path, operations in paths.items():
        for method, operation in operations.items():
            assert operation["tags"], f"{method} {path} has no tag"
            if path.startswith("/api/v1"):
                assert "401" in operation["responses"], f"{method} {path}"

    responses = {
        (method, path): set(operation["responses"])
        for path, operations in paths.items()
        for method, operation in operations.items()
    }
    assert {"404", "409"} <= responses["post", "/api/v1/fields/{field_id}/alerts"]
    assert "404" in responses["get", "/api/v1/alerts/{alert_id}"]
    assert "404" in responses["patch", "/api/v1/alerts/{alert_id}"]
    assert "404" in responses["delete", "/api/v1/alerts/{alert_id}"]
    assert "503" in responses["get", "/health/ready"]


async def test_openapi_lists_notification_statuses(client: AsyncClient) -> None:
    schemas = (await client.get("/openapi.json")).json()["components"]["schemas"]

    assert schemas["NotificationOut"]["properties"]["status"] == {
        "$ref": "#/components/schemas/NotificationStatus"
    }
    assert set(schemas["NotificationStatus"]["enum"]) == {"pending", "sent", "failed"}

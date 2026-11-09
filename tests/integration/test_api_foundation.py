from collections.abc import Callable
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
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
    assert response.json() == {"detail": "Not authenticated", "code": "unauthorized"}


@pytest.mark.parametrize(("method", "path"), BUSINESS_ROUTES)
async def test_business_route_requires_a_token(
    client: AsyncClient, method: str, path: str
) -> None:
    url = path.format(field_id=1, alert_id=1)

    response = await client.request(method, url)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["code"] == "unauthorized"


def test_every_business_route_is_collected() -> None:
    assert len(BUSINESS_ROUTES) == 7


async def test_invalid_token_returns_401(client: AsyncClient) -> None:
    response = await client.get(CATALOG, headers={"Authorization": "Bearer abc"})

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json() == {
        "detail": "Invalid or expired token",
        "code": "invalid_credentials",
    }


async def test_unknown_route_returns_404(client: AsyncClient) -> None:
    response = await client.get("/api/v1/nothing-here")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found", "code": "not_found"}


async def test_wrong_method_returns_405(client: AsyncClient) -> None:
    response = await client.post("/health/live")

    assert response.status_code == 405
    assert response.json() == {
        "detail": "Method Not Allowed",
        "code": "method_not_allowed",
    }
    # Starlette's headers on the exception are kept.
    assert response.headers["Allow"] == "GET"


async def test_unhandled_error_returns_a_generic_500(settings: Settings) -> None:
    app = create_app(settings)
    secret = "+5491100000000 password=hunter2"

    async def explode() -> None:
        raise RuntimeError(secret)

    app.add_api_route("/explode", explode)

    # The transport re-raises any exception that escapes the app, so reaching
    # the assertions also proves the server would not log it a second time.
    with capture_logs() as logs:
        async with LifespanManager(app) as manager:
            transport = ASGITransport(app=manager.app)
            async with AsyncClient(transport=transport, base_url="http://test") as http:
                response = await http.get("/explode")

    assert response.status_code == 500
    assert response.json() == {
        "detail": "Internal server error",
        "code": "internal_error",
    }
    assert "hunter2" not in response.text
    assert logs == [
        {
            "event": "api.unhandled_error",
            "error_type": "RuntimeError",
            "log_level": "error",
        }
    ]


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
    assert response.json() == {
        "detail": "Database unavailable",
        "code": "database_unavailable",
    }
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
        "detail": {"title": "Detail", "type": "string"},
        "code": {"title": "Code", "type": "string"},
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


def documented_codes(operation: dict[str, Any]) -> dict[str, set[str]]:
    return {
        status: set(response["content"]["application/json"].get("examples", {}))
        for status, response in operation["responses"].items()
        if status not in {"200", "201", "204", "422"}
    }


async def test_openapi_documents_the_codes_each_route_returns(
    client: AsyncClient,
) -> None:
    paths = (await client.get("/openapi.json")).json()["paths"]

    assert documented_codes(paths["/api/v1/fields/{field_id}/alerts"]["post"]) == {
        "401": {"invalid_credentials"},
        "404": {"field_not_found"},
        "409": {"alert_already_exists"},
    }
    for method in ("get", "patch", "delete"):
        assert documented_codes(paths["/api/v1/alerts/{alert_id}"][method]) == {
            "401": {"invalid_credentials"},
            "404": {"alert_not_found"},
        }
    assert documented_codes(paths["/api/v1/alerts"]["get"]) == {
        "401": {"invalid_credentials"}
    }
    assert documented_codes(paths["/health/ready"]["get"]) == {
        "503": {"database_unavailable"}
    }
    example = paths["/api/v1/alerts/{alert_id}"]["get"]["responses"]["404"]
    assert example["description"] == "Alert not found"
    assert example["content"]["application/json"]["examples"]["alert_not_found"][
        "value"
    ] == {"detail": "Alert not found", "code": "alert_not_found"}


async def test_openapi_lists_notification_statuses(client: AsyncClient) -> None:
    schemas = (await client.get("/openapi.json")).json()["components"]["schemas"]

    assert schemas["NotificationOut"]["properties"]["status"] == {
        "$ref": "#/components/schemas/NotificationStatus"
    }
    assert set(schemas["NotificationStatus"]["enum"]) == {"pending", "sent", "failed"}

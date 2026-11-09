from collections.abc import Callable

from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr
from structlog.testing import capture_logs

from climate_alerts.config import Settings
from climate_alerts.db import build_engine, build_session_factory

CATALOG = "/api/v1/climate-event-types"


async def test_no_token_returns_401(client: AsyncClient) -> None:
    response = await client.get(CATALOG)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


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

import structlog
from fastapi import APIRouter
from sqlalchemy import select

from climate_alerts.api.dependencies import SessionDep
from climate_alerts.api.errors import error_responses
from climate_alerts.errors import DatabaseUnavailable

logger = structlog.get_logger()

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", responses=error_responses(DatabaseUnavailable))
async def ready(session: SessionDep) -> dict[str, str]:
    # Broad on purpose: connection failures surface as raw OSError or asyncpg
    # exceptions rather than SQLAlchemyError, and any failure means not ready.
    try:
        await session.execute(select(1))
    except Exception as exc:
        # The type only: driver messages can carry connection details.
        logger.warning("health.ready_failed", error_type=type(exc).__name__)
        raise DatabaseUnavailable from exc
    return {"status": "ok"}

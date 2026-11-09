from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from climate_alerts.api.dependencies import SessionDep

router = APIRouter(prefix="/health")


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(session: SessionDep) -> dict[str, str]:
    # Broad on purpose: connection failures surface as raw OSError or asyncpg
    # exceptions rather than SQLAlchemyError, and any failure means not ready.
    try:
        await session.execute(select(1))
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable"
        ) from exc
    return {"status": "ok"}

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from climate_alerts.agrobot.router import router as agrobot_router
from climate_alerts.alerts.router import router as alerts_router
from climate_alerts.api.errors import register_error_handlers
from climate_alerts.api.health import router as health_router
from climate_alerts.config import Settings, get_settings
from climate_alerts.db import build_engine, build_session_factory
from climate_alerts.logs import configure_logging
from climate_alerts.notifications.router import router as notifications_router


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    # Here and not in the lifespan: uvicorn calls this factory before its first
    # log line, so that line and every later one are JSON.
    configure_logging(resolved.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = build_engine(resolved)
        app.state.settings = resolved
        app.state.sessions = build_session_factory(engine)
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(title="Climate Alert System", lifespan=lifespan)
    register_error_handlers(app)
    app.include_router(health_router)
    app.include_router(agrobot_router, prefix="/api/v1")
    app.include_router(alerts_router, prefix="/api/v1")
    app.include_router(notifications_router, prefix="/api/v1")
    return app

import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from climate_alerts.agrobot.router import router as agrobot_router
from climate_alerts.alerts.router import router as alerts_router
from climate_alerts.api.health import router as health_router
from climate_alerts.config import Settings, get_settings
from climate_alerts.db import build_engine, build_session_factory
from climate_alerts.logs import configure_logging


def _renderable(value: object) -> object:
    """Reduce a validation error value to something JSON can always render.

    A lone surrogate cannot be encoded as UTF-8 and a non-finite float is not
    JSON. Both reach here from request bodies, including through `loc` when
    they are a body key, so each is replaced by readable text.
    """
    if isinstance(value, str):
        return value.encode("utf-8", "backslashreplace").decode("utf-8")
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, dict):
        return {_renderable(str(k)): _renderable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_renderable(item) for item in value]
    try:
        return _renderable(jsonable_encoder(value))
    except TypeError, ValueError:
        return _renderable(str(value))


def _validation_error_body(exc: RequestValidationError) -> dict[str, object]:
    # `input` is dropped because echoing it is what makes rendering fail: the
    # rejected value is exactly the one JSON cannot represent.
    return {
        "detail": [
            _renderable({k: v for k, v in error.items() if k != "input"})
            for error in exc.errors()
        ]
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved = settings or get_settings()
        configure_logging(resolved.log_level)
        engine = build_engine(resolved)
        app.state.settings = resolved
        app.state.sessions = build_session_factory(engine)
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(title="Climate Alert System", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            _validation_error_body(exc),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    app.include_router(health_router)
    app.include_router(agrobot_router, prefix="/api/v1")
    app.include_router(alerts_router, prefix="/api/v1")
    return app

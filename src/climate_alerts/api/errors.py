"""How every error becomes an HTTP response, and how routes document them.

HTTP errors are `{"detail": message, "code": code}`. Validation errors keep
FastAPI's list shape, `{"detail": [{"type", "loc", "msg"}]}`.
"""

import http
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import structlog
from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from climate_alerts import errors
from climate_alerts.api.schemas import ErrorOut
from climate_alerts.errors import DomainError

logger = structlog.get_logger()


@dataclass(frozen=True)
class HttpError:
    """The response for one `DomainError`.

    With `loc`, the error is a failed check on that request field, and it is
    rendered in the validation list shape so that every 422 has one shape.
    """

    status: int
    loc: tuple[str, ...] | None = None


HTTP_ERRORS: dict[type[DomainError], HttpError] = {
    errors.FieldNotFound: HttpError(status.HTTP_404_NOT_FOUND),
    errors.AlertNotFound: HttpError(status.HTTP_404_NOT_FOUND),
    errors.AlertAlreadyExists: HttpError(status.HTTP_409_CONFLICT),
    errors.UnknownEventType: HttpError(
        status.HTTP_422_UNPROCESSABLE_CONTENT, loc=("body", "event_type")
    ),
    errors.InvalidCredentials: HttpError(status.HTTP_401_UNAUTHORIZED),
    errors.DatabaseUnavailable: HttpError(status.HTTP_503_SERVICE_UNAVAILABLE),
}

_BEARER_CHALLENGE = {"WWW-Authenticate": "Bearer"}

_INTERNAL_ERROR = {"detail": "Internal server error", "code": "internal_error"}


def _validation_response(issues: Iterable[Mapping[str, Any]]) -> Response:
    """Render only `type`, `loc` and `msg`.

    `input` and `ctx` can hold the rejected value, and that value may be what
    JSON cannot represent: a non-finite float, or a lone surrogate. `json.dumps`
    escapes surrogates, which an encoder writing UTF-8 would fail on.
    """
    body = json.dumps(
        {
            "detail": [
                {"type": issue["type"], "loc": issue["loc"], "msg": issue["msg"]}
                for issue in issues
            ]
        }
    )
    return Response(
        body,
        media_type="application/json",
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
    )


def _status_code_name(status_code: int) -> str:
    """`404` -> `not_found`, from the standard reason phrase."""
    phrase = http.HTTPStatus(status_code).phrase
    return re.sub(r"[^a-z0-9]+", "_", phrase.lower()).strip("_")


class _UnhandledErrorMiddleware:
    """Answer an exception no handler took with a generic 500, logged once.

    Not an `Exception` handler: Starlette's `ServerErrorMiddleware` calls that
    handler and then re-raises, so the server would log the same error again,
    with its message and traceback. Sitting inside it, this answers instead
    and does not re-raise. If the response has already started, no JSON body
    can follow, so it re-raises and the server closes the connection.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:
            if response_started:
                raise
            # The type only: the message can carry request or database values.
            logger.error("api.unhandled_error", error_type=type(exc).__name__)
            response = JSONResponse(
                _INTERNAL_ERROR, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
            await response(scope, receive, send)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def handle_domain_error(_request: Request, exc: DomainError) -> Response:
        mapping = HTTP_ERRORS[type(exc)]
        if mapping.loc is not None:
            return _validation_response(
                [{"type": exc.code, "loc": mapping.loc, "msg": exc.message}]
            )
        headers = (
            _BEARER_CHALLENGE
            if mapping.status == status.HTTP_401_UNAUTHORIZED
            else None
        )
        return JSONResponse(
            {"detail": exc.message, "code": exc.code},
            status_code=mapping.status,
            headers=headers,
        )

    # Starlette's class, which FastAPI's subclasses: it also covers the
    # router's own 404 and 405, and HTTPBearer's missing-token 401.
    @app.exception_handler(HTTPException)
    async def handle_http_exception(_request: Request, exc: HTTPException) -> Response:
        return JSONResponse(
            {"detail": exc.detail, "code": _status_code_name(exc.status_code)},
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> Response:
        return _validation_response(exc.errors())

    app.add_middleware(_UnhandledErrorMiddleware)


def error_responses(*catalog: type[DomainError]) -> dict[int | str, dict[str, Any]]:
    """The OpenAPI `responses` for the errors a route can return.

    Status, description and example code come from the catalog. Errors that
    render as a field validation failure add nothing: FastAPI documents the
    validation 422 itself, and declaring 422 here would replace that entry.
    """
    by_status: dict[int, list[type[DomainError]]] = {}
    for error in catalog:
        mapping = HTTP_ERRORS[error]
        if mapping.loc is None:
            by_status.setdefault(mapping.status, []).append(error)

    return {
        status_code: {
            "model": ErrorOut,
            "description": "; ".join(error.message for error in group),
            "content": {
                "application/json": {
                    "examples": {
                        error.code: {
                            "summary": error.message,
                            "value": {"detail": error.message, "code": error.code},
                        }
                        for error in group
                    }
                }
            },
        }
        for status_code, group in by_status.items()
    }

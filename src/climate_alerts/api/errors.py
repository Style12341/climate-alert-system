"""How every error becomes an HTTP response, and how routes document them.

HTTP errors are `{"detail": message, "code": code}`. Validation errors keep
FastAPI's list shape, `{"detail": [{"type", "loc", "msg"}]}`.
"""

import http
import json
import re
import traceback
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import structlog
from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from climate_alerts import errors
from climate_alerts.errors import DomainError

logger = structlog.get_logger()


class ErrorOut(BaseModel):
    detail: str
    code: str


@dataclass(frozen=True)
class ErrorStatus:
    """The HTTP status of one `DomainError`, and whether it is a field check.

    Without `loc`, the error renders as `{"detail", "code"}` and
    `error_responses` lists it in `/docs`. With `loc`, it is a failed check on
    that request field: it renders in the validation list shape, so that every
    422 has one shape, and `/docs` does not list it as a JSON error, because
    FastAPI's own validation 422 already documents that shape.
    """

    status: int
    loc: tuple[str, ...] | None = None


HTTP_ERRORS: dict[type[DomainError], ErrorStatus] = {
    errors.FieldNotFound: ErrorStatus(status.HTTP_404_NOT_FOUND),
    errors.AlertNotFound: ErrorStatus(status.HTTP_404_NOT_FOUND),
    errors.AlertAlreadyExists: ErrorStatus(status.HTTP_409_CONFLICT),
    errors.UnknownEventType: ErrorStatus(
        status.HTTP_422_UNPROCESSABLE_CONTENT, loc=("body", "event_type")
    ),
    errors.InvalidCredentials: ErrorStatus(status.HTTP_401_UNAUTHORIZED),
    errors.DatabaseUnavailable: ErrorStatus(status.HTTP_503_SERVICE_UNAVAILABLE),
}

_BEARER_CHALLENGE = {"WWW-Authenticate": "Bearer"}

_INTERNAL_ERROR = {"detail": "Internal server error", "code": "internal_error"}

# HTTPBearer's own 401 when the header is missing, which `handle_http_exception`
# renders. Every route that takes a token can return it next to the catalog's.
_MISSING_TOKEN = {"detail": "Not authenticated", "code": "unauthorized"}


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


def _error_context(exc: Exception, scope: Scope) -> dict[str, str]:
    """Where an unhandled error happened, without any request or database value.

    Never the message or a traceback: the message can carry request values,
    and a database error's chained asyncpg exception appends PostgreSQL's
    DETAIL line, with the value a constraint rejected. Never the raw path
    either: it holds ids. The route's path template holds none.
    """
    context = {"error_type": type(exc).__name__}
    frames = traceback.extract_tb(exc.__traceback__)
    if frames:
        frame = frames[-1]
        context["error_location"] = f"{frame.filename}:{frame.lineno}:{frame.name}"
    context["method"] = scope["method"]
    route = scope.get("route")
    if route is not None and hasattr(route, "path"):
        context["route"] = route.path
    return context


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
            logger.error("api.unhandled_error", **_error_context(exc, scope))
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


def error_responses(*errors: type[DomainError]) -> dict[int | str, dict[str, Any]]:
    """The OpenAPI `responses` for the errors a route can return.

    Status, description and example code come from the catalog. Errors that
    render as a field validation failure add nothing: FastAPI documents the
    validation 422 itself, and declaring 422 here would replace that entry.
    A 401 also lists HTTPBearer's missing-token error, which is not in the
    catalog.
    """
    by_status: dict[int, list[dict[str, str]]] = {}
    for error in errors:
        mapping = HTTP_ERRORS[error]
        if mapping.loc is None:
            by_status.setdefault(mapping.status, []).append(
                {"detail": error.message, "code": error.code}
            )
    if status.HTTP_401_UNAUTHORIZED in by_status:
        by_status[status.HTTP_401_UNAUTHORIZED].insert(0, dict(_MISSING_TOKEN))

    return {
        status_code: {
            "model": ErrorOut,
            "description": "; ".join(body["detail"] for body in bodies),
            "content": {
                "application/json": {
                    "examples": {
                        body["code"]: {"summary": body["detail"], "value": body}
                        for body in bodies
                    }
                }
            },
        }
        for status_code, bodies in by_status.items()
    }

"""Every application error, with a stable code and a client-facing message.

The catalog knows nothing about HTTP: `api/errors.py` maps each error to its
response. Messages are English, sentence case and without a trailing period,
like FastAPI's own, and must never carry a value from the request or the
database.

Internal errors are logged, never returned to the client. The only texts a
client sees are fixed ones: a message from this module, `last_error` included;
`api/errors.py`'s "Internal server error"; the `HTTPException` details of
FastAPI and Starlette, such as "Not authenticated", "Not Found" and "Method Not
Allowed"; pydantic's validation messages, built from the schema and never
from the rejected value; and the notifications router's `limit_too_large`
message.
"""

from enum import Enum
from typing import ClassVar


class DomainError(Exception):
    code: ClassVar[str]
    message: ClassVar[str]


class FieldNotFound(DomainError):
    code = "field_not_found"
    message = "Field not found"


class AlertNotFound(DomainError):
    code = "alert_not_found"
    message = "Alert not found"


class AlertAlreadyExists(DomainError):
    code = "alert_already_exists"
    message = "An active alert already exists for this field and event type"


class UnknownEventType(DomainError):
    code = "unknown_event_type"
    message = "Unknown event type"


class InvalidCredentials(DomainError):
    """The bearer token cannot be trusted or does not name a valid user id."""

    code = "invalid_credentials"
    message = "Invalid or expired token"


class DatabaseUnavailable(DomainError):
    code = "database_unavailable"
    message = "Database unavailable"


class DeliveryFailure(Enum):
    """Why a notification was not delivered, the only text `last_error` stores.

    `GET /api/v1/notifications` returns `last_error`, so it never holds an
    exception's message or class name, provider text or configuration. The
    internal cause is logged as `dispatch.send_failed`.
    """

    code: str
    message: str

    NO_ADDRESS = (
        "recipient_has_no_address",
        "Recipient has no address for this channel",
    )
    CHANNEL_UNAVAILABLE = ("channel_unavailable", "Channel not available")
    TIMED_OUT = ("delivery_timed_out", "Delivery timed out")
    ATTEMPTS_EXHAUSTED = ("attempts_exhausted", "Delivery attempts exhausted")
    FAILED = ("delivery_failed", "Delivery failed")

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message

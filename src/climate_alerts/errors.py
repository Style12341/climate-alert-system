"""Every application error, with a stable code and a client-facing message.

The catalog knows nothing about HTTP: `api/errors.py` maps each error to its
response. Messages are English, sentence case and without a trailing period,
like FastAPI's own, and must never carry a value from the request or the
database.
"""

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

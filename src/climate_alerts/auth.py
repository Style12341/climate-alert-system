import re

import jwt

from climate_alerts.config import Settings
from climate_alerts.errors import InvalidCredentials

# Largest value of a PostgreSQL BIGINT, the type of every primary key.
MAX_BIGINT = 2**63 - 1

# int() also accepts spaces, underscores and non-ASCII digits, so it cannot
# decide what a user id looks like.
_USER_ID = re.compile(r"[1-9][0-9]*")


def verify_token(token: str, settings: Settings) -> int:
    """Return the user id in a signed token, or raise InvalidCredentials."""
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub"]},
        )
        subject = claims["sub"]
        if _USER_ID.fullmatch(subject) is None:
            raise InvalidCredentials
        # Above Python's digit limit int() raises ValueError, so it stays here.
        user_id = int(subject)
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise InvalidCredentials from exc

    if user_id > MAX_BIGINT:
        raise InvalidCredentials
    return user_id

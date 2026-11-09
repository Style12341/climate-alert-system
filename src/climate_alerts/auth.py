import jwt

from climate_alerts.config import Settings

# Largest value of a PostgreSQL BIGINT, the type of every primary key.
MAX_BIGINT = 2**63 - 1


class InvalidCredentials(Exception):
    """The bearer token cannot be trusted or does not name a valid user id."""


def verify_token(token: str, settings: Settings) -> int:
    """Return the user id in a signed token, or raise InvalidCredentials."""
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub"]},
        )
        user_id = int(claims["sub"])
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise InvalidCredentials from exc

    if not 1 <= user_id <= MAX_BIGINT:
        raise InvalidCredentials
    return user_id

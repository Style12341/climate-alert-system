from datetime import UTC, datetime, timedelta

import jwt

from climate_alerts.config import Settings


def issue_token(user_id: int, settings: Settings, ttl: timedelta) -> str:
    """Sign a token for a user, standing in for the pre-existing login system."""
    claims = {"sub": str(user_id), "exp": datetime.now(UTC) + ttl}
    return jwt.encode(
        claims,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )

from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Field names map to env vars with no prefix.

    Every `*_seconds` field is a float so tests can use sub-second values.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Required, no default: the application refuses to start without them.
    database_url: str
    jwt_secret: Annotated[SecretStr, Field(min_length=32)]

    jwt_algorithm: str = "HS256"

    evaluation_interval_seconds: float = 300
    evaluation_chunk_size: int = 5000

    # Kept as a plain string: pydantic-settings would expect JSON for a
    # list field, but this variable is comma-separated. See enabled_channels.
    notification_channels: str = "whatsapp"

    dispatch_interval_seconds: float = 5
    dispatch_batch_size: int = 500
    dispatch_concurrency: int = 40
    dispatch_send_timeout_seconds: float = 10
    dispatch_lease_seconds: float = 300
    dispatch_max_attempts: int = 8
    dispatch_backoff_base_seconds: float = 60
    dispatch_backoff_cap_seconds: float = 3600

    default_page_size: int = 50
    max_page_size: int = 200

    db_pool_size: int = 5
    db_max_overflow: int = 5

    worker_shutdown_timeout_seconds: float = 25
    seed_token_ttl_hours: float = 24
    log_level: str = "INFO"

    @field_validator("notification_channels")
    @classmethod
    def _require_a_channel(cls, value: str) -> str:
        if not _parse_channels(value):
            raise ValueError("NOTIFICATION_CHANNELS must name at least one channel")
        return value

    @property
    def enabled_channels(self) -> tuple[str, ...]:
        return _parse_channels(self.notification_channels)


def _parse_channels(raw: str) -> tuple[str, ...]:
    """Split on commas, strip, drop empties, deduplicate keeping first-seen order."""
    names = (part.strip() for part in raw.split(","))
    return tuple(dict.fromkeys(name for name in names if name))


@lru_cache
def get_settings() -> Settings:
    return Settings()  # required fields come from the environment

from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PositiveCount = Annotated[int, Field(gt=0)]
PositiveSeconds = Annotated[float, Field(gt=0)]


class Settings(BaseSettings):
    """Runtime configuration. Field names map to env vars with no prefix.

    Every `*_seconds` field is a float so tests can use sub-second values.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Required, no default: the application refuses to start without them.
    database_url: SecretStr
    jwt_secret: Annotated[SecretStr, Field(min_length=32)]

    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"

    evaluation_interval_seconds: PositiveSeconds = 300
    evaluation_chunk_size: PositiveCount = 5000

    # Kept as a plain string: pydantic-settings would expect JSON for a
    # list field, but this variable is comma-separated. See enabled_channels.
    notification_channels: str = "whatsapp"

    dispatch_interval_seconds: PositiveSeconds = 5
    dispatch_batch_size: PositiveCount = 500
    dispatch_concurrency: PositiveCount = 40
    dispatch_send_timeout_seconds: PositiveSeconds = 10
    dispatch_lease_seconds: PositiveSeconds = 300
    dispatch_max_attempts: PositiveCount = 8
    dispatch_backoff_base_seconds: PositiveSeconds = 60
    dispatch_backoff_cap_seconds: PositiveSeconds = 3600

    db_pool_size: PositiveCount = 5
    # Zero is a real choice here: no connections beyond the pool.
    db_max_overflow: Annotated[int, Field(ge=0)] = 5
    db_command_timeout_seconds: PositiveSeconds = 60

    worker_shutdown_timeout_seconds: PositiveSeconds = 25
    log_level: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"] = "INFO"

    @field_validator("notification_channels")
    @classmethod
    def _require_a_channel(cls, value: str) -> str:
        if not _parse_channels(value):
            raise ValueError("NOTIFICATION_CHANNELS must name at least one channel")
        return value

    @model_validator(mode="after")
    def _check_lease_outlasts_a_batch(self) -> Self:
        slowest_batch = (
            self.dispatch_batch_size
            / self.dispatch_concurrency
            * self.dispatch_send_timeout_seconds
        )
        if self.dispatch_lease_seconds <= slowest_batch:
            # A shorter lease expires mid-batch, so rows are claimed and sent twice.
            raise ValueError(
                "dispatch_lease_seconds must exceed the slowest batch "
                f"({slowest_batch} s)"
            )
        return self

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

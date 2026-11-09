import pytest
from pydantic import ValidationError

from climate_alerts.config import Settings

DB_URL = "postgresql+asyncpg://u:p@localhost/db"
SECRET = "s" * 32


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    monkeypatch.setenv("JWT_SECRET", SECRET)


def test_requires_database_url_and_secret() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_rejects_short_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    monkeypatch.setenv("JWT_SECRET", "x" * 31)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.usefixtures("required_env")
def test_defaults_match_spec() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.evaluation_interval_seconds == 300
    assert s.evaluation_chunk_size == 5000
    assert s.dispatch_interval_seconds == 5
    assert s.dispatch_batch_size == 500
    assert s.dispatch_concurrency == 40
    assert s.dispatch_send_timeout_seconds == 10
    assert s.dispatch_lease_seconds == 300
    assert s.dispatch_max_attempts == 8
    assert s.dispatch_backoff_base_seconds == 60
    assert s.dispatch_backoff_cap_seconds == 3600
    assert s.jwt_algorithm == "HS256"
    assert s.notification_channels == "whatsapp"
    assert s.enabled_channels == ("whatsapp",)
    assert s.db_pool_size == 5
    assert s.db_max_overflow == 5
    assert s.db_command_timeout_seconds == 60
    assert s.worker_shutdown_timeout_seconds == 25
    assert s.log_level == "INFO"


@pytest.mark.usefixtures("required_env")
def test_environment_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVALUATION_CHUNK_SIZE", "10")
    assert Settings(_env_file=None).evaluation_chunk_size == 10  # type: ignore[call-arg]


@pytest.mark.usefixtures("required_env")
def test_channels_are_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFICATION_CHANNELS", " whatsapp, email ,whatsapp")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.enabled_channels == ("whatsapp", "email")


@pytest.mark.usefixtures("required_env")
def test_empty_channels_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFICATION_CHANNELS", " , ")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def build(**overrides: object) -> Settings:
    values: dict[str, object] = {"database_url": DB_URL, "jwt_secret": SECRET}
    return Settings(_env_file=None, **(values | overrides))  # type: ignore[arg-type]


def test_repr_does_not_expose_the_database_password() -> None:
    settings = build(database_url="postgresql+asyncpg://user:hunter2@host/db")

    assert "hunter2" not in repr(settings)
    assert "hunter2" in settings.database_url.get_secret_value()


def test_rejects_unsupported_jwt_algorithm() -> None:
    with pytest.raises(ValidationError):
        build(jwt_algorithm="none")


def test_accepts_another_hmac_jwt_algorithm() -> None:
    assert build(jwt_algorithm="HS512").jwt_algorithm == "HS512"


def test_rejects_unknown_log_level() -> None:
    with pytest.raises(ValidationError):
        build(log_level="LOUD")


@pytest.mark.parametrize(
    "field",
    [
        "evaluation_interval_seconds",
        "evaluation_chunk_size",
        "dispatch_interval_seconds",
        "dispatch_batch_size",
        "dispatch_concurrency",
        "dispatch_send_timeout_seconds",
        "dispatch_lease_seconds",
        "dispatch_max_attempts",
        "dispatch_backoff_base_seconds",
        "dispatch_backoff_cap_seconds",
        "db_pool_size",
        "db_command_timeout_seconds",
        "worker_shutdown_timeout_seconds",
    ],
)
def test_rejects_non_positive_value(field: str) -> None:
    with pytest.raises(ValidationError):
        build(**{field: 0})


def test_max_overflow_may_be_zero() -> None:
    assert build(db_max_overflow=0).db_max_overflow == 0


def test_rejects_negative_max_overflow() -> None:
    with pytest.raises(ValidationError):
        build(db_max_overflow=-1)


def test_rejects_lease_shorter_than_the_slowest_batch() -> None:
    # 500 / 40 x 10 s = 125 s, and the lease must exceed it.
    with pytest.raises(ValidationError, match="dispatch_lease_seconds"):
        build(dispatch_lease_seconds=125)


def test_accepts_lease_above_the_slowest_batch() -> None:
    assert build(dispatch_lease_seconds=125.5).dispatch_lease_seconds == 125.5

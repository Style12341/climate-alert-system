import pytest
from pydantic import ValidationError

from climate_alerts.config import Settings

DB_URL = "postgresql+asyncpg://u:p@localhost/db"
SECRET = "s" * 32


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "DATABASE_URL",
        "JWT_SECRET",
        "EVALUATION_CHUNK_SIZE",
        "NOTIFICATION_CHANNELS",
    ):
        monkeypatch.delenv(name, raising=False)


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
    assert s.default_page_size == 50
    assert s.max_page_size == 200
    assert s.jwt_algorithm == "HS256"
    assert s.enabled_channels == ("whatsapp",)


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

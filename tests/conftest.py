from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from testcontainers.community.postgres import PostgresContainer

from climate_alerts.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]


def alembic_config(url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    # ConfigParser treats "%" as interpolation, so a literal one must be doubled.
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """Start PostgreSQL and build the schema with the real migration.

    Sync on purpose: Alembic's async env.py calls asyncio.run, which cannot
    run inside the loop that async fixtures and tests use.
    """
    with PostgresContainer("postgres:17", driver="asyncpg") as postgres:
        url = postgres.get_connection_url()
        command.upgrade(alembic_config(url), "head")
        yield url


@pytest.fixture(scope="session")
def settings(postgres_url: str) -> Settings:
    return Settings(_env_file=None, database_url=postgres_url, jwt_secret="x" * 32)

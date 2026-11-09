import asyncio
import os
import sys
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from climate_alerts.agrobot.models import Field, User, WeatherForecast
from climate_alerts.agrobot.seed import seed
from climate_alerts.alerts.models import Alert
from climate_alerts.auth import verify_token
from climate_alerts.config import Settings
from climate_alerts.db import SessionFactory
from climate_alerts.notifications.evaluation import evaluate_alerts
from climate_alerts.notifications.models import Notification

SOME_DAY = date(2030, 6, 15)
FIELD_TIMEZONES = {
    "Buenos Aires": "America/Argentina/Buenos_Aires",
    "Montevideo": "America/Montevideo",
    "Bogotá": "America/Bogota",
    "Mexico City": "America/Mexico_City",
    "São Paulo": "America/Sao_Paulo",
}
ForecastKey = tuple[str, date, str]


def scenario_rows(today: date) -> dict[ForecastKey, Decimal]:
    return {
        ("Buenos Aires", today + timedelta(days=1), "frost"): Decimal("85.00"),
        ("Montevideo", today + timedelta(days=2), "frost"): Decimal("90.00"),
    }


async def row_count(sessions: SessionFactory, model: type) -> int:
    async with sessions() as session:
        count = await session.scalar(select(func.count()).select_from(model))
    assert count is not None
    return count


async def stored_forecasts(sessions: SessionFactory) -> dict[ForecastKey, Decimal]:
    async with sessions() as session:
        rows = await session.execute(
            select(
                Field.name,
                WeatherForecast.forecast_date,
                WeatherForecast.event_type,
                WeatherForecast.probability,
            ).join(Field, Field.id == WeatherForecast.field_id)
        )
        return {(name, day, event): value for name, day, event, value in rows}


async def stored_ids(sessions: SessionFactory) -> list[Sequence[int]]:
    async with sessions() as session:
        return [
            (await session.scalars(select(model.id).order_by(model.id))).all()
            for model in (User, Field, Alert)
        ]


async def test_creates_expected_data(
    sessions: SessionFactory, settings: Settings
) -> None:
    await seed(sessions, settings, today=SOME_DAY)

    assert await row_count(sessions, User) == 3
    assert await row_count(sessions, Field) == 5
    assert await row_count(sessions, WeatherForecast) == 5 * 16 * 7
    async with sessions() as session:
        users = (await session.scalars(select(User))).all()
        fields = (await session.scalars(select(Field).order_by(Field.id))).all()
    assert sum(user.email is None for user in users) == 1
    assert {f.name: f.timezone for f in fields} == FIELD_TIMEZONES
    assert [f.name for f in fields] == list(FIELD_TIMEZONES)


async def test_forecasts_cover_sixteen_days_from_today(
    sessions: SessionFactory, settings: Settings
) -> None:
    await seed(sessions, settings, today=SOME_DAY)

    days = {day for _, day, _ in await stored_forecasts(sessions)}

    assert days == {SOME_DAY + timedelta(days=offset) for offset in range(16)}


async def test_today_defaults_to_each_field_local_date(
    sessions: SessionFactory, settings: Settings
) -> None:
    # Read on both sides so a local midnight during the run cannot fail the test.
    def local_dates() -> dict[str, date]:
        return {
            name: datetime.now(ZoneInfo(zone)).date()
            for name, zone in FIELD_TIMEZONES.items()
        }

    before = local_dates()
    await seed(sessions, settings)
    after = local_dates()

    first_day: dict[str, date] = {}
    for name, day, _ in await stored_forecasts(sessions):
        first_day[name] = min(day, first_day.get(name, day))
    for name, day in first_day.items():
        assert day in {before[name], after[name]}


async def test_is_idempotent(sessions: SessionFactory, settings: Settings) -> None:
    first = await seed(sessions, settings, today=SOME_DAY)
    ids_after_first = await stored_ids(sessions)
    forecasts_after_first = await row_count(sessions, WeatherForecast)

    second = await seed(sessions, settings, today=SOME_DAY)

    assert await stored_ids(sessions) == ids_after_first
    assert await row_count(sessions, WeatherForecast) == forecasts_after_first
    assert [(u.id, u.field_ids, u.has_email) for u in first] == [
        (u.id, u.field_ids, u.has_email) for u in second
    ]


async def test_rerun_refreshes_fetched_at(
    sessions: SessionFactory, settings: Settings
) -> None:
    async def fetched_at() -> datetime:
        async with sessions() as session:
            value = await session.scalar(select(func.min(WeatherForecast.fetched_at)))
        assert value is not None
        return value

    await seed(sessions, settings, today=SOME_DAY)
    first = await fetched_at()

    await seed(sessions, settings, today=SOME_DAY)

    assert await fetched_at() > first


async def test_is_deterministic(sessions: SessionFactory, settings: Settings) -> None:
    await seed(sessions, settings, today=SOME_DAY)
    first = await stored_forecasts(sessions)

    await seed(sessions, settings, today=SOME_DAY)

    assert await stored_forecasts(sessions) == first


async def test_value_does_not_depend_on_the_day_it_is_seeded(
    sessions: SessionFactory, settings: Settings
) -> None:
    await seed(sessions, settings, today=SOME_DAY)
    first = await stored_forecasts(sessions)

    next_day = SOME_DAY + timedelta(days=1)
    await seed(sessions, settings, today=next_day)
    second = await stored_forecasts(sessions)

    moved_scenario = {*scenario_rows(SOME_DAY), *scenario_rows(next_day)}
    overlap = [key for key in first if key[1] >= next_day and key not in moved_scenario]
    assert len(overlap) > 500
    assert all(first[key] == second[key] for key in overlap)


async def test_background_stays_below_thresholds(
    sessions: SessionFactory, settings: Settings
) -> None:
    await seed(sessions, settings, today=SOME_DAY)
    forecasts = await stored_forecasts(sessions)
    scenario = scenario_rows(SOME_DAY)

    background = [v for key, v in forecasts.items() if key not in scenario]

    assert len(background) == 5 * 16 * 7 - 2
    assert all(0 <= value <= 40 for value in background)
    assert {key: forecasts[key] for key in scenario} == scenario


async def test_alerts_are_frost_on_every_field_and_rain_on_one(
    sessions: SessionFactory, settings: Settings
) -> None:
    await seed(sessions, settings, today=SOME_DAY)

    async with sessions() as session:
        alerts = (
            await session.execute(select(Alert.event_type, Alert.threshold))
        ).all()

    assert sorted(alerts) == [
        *[("frost", Decimal("70.00"))] * 5,
        ("rain", Decimal("80.00")),
    ]


@pytest.mark.parametrize("channels", ["whatsapp", "whatsapp,email"])
async def test_scenario_produces_notifications(
    sessions: SessionFactory, settings: Settings, channels: str
) -> None:
    configured = settings.model_copy(update={"notification_channels": channels})
    await seed(sessions, configured)

    result = await evaluate_alerts(sessions, configured, asyncio.Event())

    assert result.created == 2 * len(configured.enabled_channels)
    async with sessions() as session:
        rows = await session.execute(
            select(Field.name, Alert.event_type, Notification.probability)
            .join(Alert, Alert.id == Notification.alert_id)
            .join(Field, Field.id == Alert.field_id)
        )
        fired = set(rows.all())
    # The rain alert at 80 stays silent, even on a field with frost at 85.
    assert fired == {
        ("Buenos Aires", "frost", Decimal("85.00")),
        ("Montevideo", "frost", Decimal("90.00")),
    }


async def test_user_without_email_is_in_the_scenario(
    sessions: SessionFactory, settings: Settings
) -> None:
    seeded = await seed(sessions, settings)
    await evaluate_alerts(sessions, settings, asyncio.Event())

    async with sessions() as session:
        owners = await session.execute(
            select(Field.name, User.id, User.email)
            .join(User, User.id == Field.owner_id)
            .where(Field.name.in_(["Buenos Aires", "Montevideo"]))
        )
        by_field = {name: (owner, email) for name, owner, email in owners}

    assert [user.has_email for user in seeded] == [True, False, True]
    assert by_field["Montevideo"] == (seeded[1].id, None)
    assert by_field["Buenos Aires"][0] == seeded[0].id
    assert by_field["Buenos Aires"][1] is not None
    assert len(seeded[0].field_ids) > 1


async def test_tokens_verify(sessions: SessionFactory, settings: Settings) -> None:
    seeded = await seed(sessions, settings, today=SOME_DAY)

    assert all(verify_token(user.token, settings) == user.id for user in seeded)


def command_environment(database_url: str, secret: str) -> dict[str, str]:
    return {**os.environ, "DATABASE_URL": database_url, "JWT_SECRET": secret}


async def run_seed_command(
    environment: dict[str, str], cwd: Path
) -> tuple[int, str, str]:
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "climate_alerts.agrobot.seed",
        cwd=cwd,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await child.communicate()
    assert child.returncode is not None
    return child.returncode, stdout.decode(), stderr.decode()


async def test_command_prints_credentials_without_contact_details(
    sessions: SessionFactory, settings: Settings, postgres_url: str, tmp_path: Path
) -> None:
    # In a temporary directory so a developer's .env cannot change the run.
    code, stdout, stderr = await run_seed_command(
        command_environment(postgres_url, "x" * 32), tmp_path
    )

    assert code == 0, stderr
    async with sessions() as session:
        users = (await session.scalars(select(User).order_by(User.id))).all()
        fields = (await session.scalars(select(Field))).all()
    printed = [line.split() for line in stdout.splitlines()]
    assert [int(words[1]) for words in printed] == [user.id for user in users]
    assert [verify_token(words[5], settings) for words in printed] == [
        user.id for user in users
    ]
    assert {int(id_) for words in printed for id_ in words[3].split(",")} == {
        field.id for field in fields
    }
    for user in users:
        assert user.phone not in stdout + stderr
        assert user.email is None or user.email not in stdout + stderr


async def test_command_fails_with_non_zero_exit(tmp_path: Path) -> None:
    unreachable = "postgresql+asyncpg://nobody:hunter2@127.0.0.1:1/none"

    code, stdout, stderr = await run_seed_command(
        command_environment(unreachable, "x" * 32), tmp_path
    )

    assert code != 0
    assert "hunter2" not in stdout + stderr

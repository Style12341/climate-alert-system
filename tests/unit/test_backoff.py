from random import Random

from climate_alerts.notifications.dispatch import backoff_seconds


def seeded() -> Random:
    return Random(0)  # noqa: S311 - reproducible jitter, not security


def test_backoff_range() -> None:
    assert 30 <= backoff_seconds(1, 60, 3600, seeded()) <= 60


def test_backoff_doubles_with_each_attempt() -> None:
    first = backoff_seconds(1, 60, 3600, seeded())
    third = backoff_seconds(3, 60, 3600, seeded())

    assert third == first * 4


def test_backoff_is_capped() -> None:
    assert 1800 <= backoff_seconds(20, 60, 3600, seeded()) <= 3600

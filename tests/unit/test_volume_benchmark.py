import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "volume_benchmark.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("volume_benchmark", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://u:p@db.internal/x",
        # The asyncpg dialect lets these override the host in the URL.
        "postgresql+asyncpg://u:p@127.0.0.1/x?host=db.internal",
        "postgresql+asyncpg://u:p@127.0.0.1/x?port=6543",
        "postgresql+asyncpg://u:p@127.0.0.1/x?host=127.0.0.1&port=5432",
    ],
)
def test_guard_refuses_a_url_that_may_not_be_local(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("BENCHMARK_DATABASE_URL", url)

    with pytest.raises(SystemExit, match="refusing"):
        load_script().checked_database_url()


def test_guard_accepts_a_local_url(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "postgresql+asyncpg://u:p@127.0.0.1:55432/x"
    monkeypatch.setenv("BENCHMARK_DATABASE_URL", url)

    assert load_script().checked_database_url().host == "127.0.0.1"

import ast
import inspect
import re
from pathlib import Path

from climate_alerts import errors
from climate_alerts.api.errors import HTTP_ERRORS
from climate_alerts.errors import DomainError

SRC = Path(__file__).resolve().parents[2] / "src" / "climate_alerts"

# Discovered from the module, so an error added later is covered without
# editing this list.
DOMAIN_ERRORS = [
    member
    for _, member in inspect.getmembers(errors, inspect.isclass)
    if issubclass(member, DomainError) and member is not DomainError
]


def test_the_catalog_is_discovered() -> None:
    assert len(DOMAIN_ERRORS) == 6


def test_every_domain_error_has_a_status() -> None:
    missing = [error.__name__ for error in DOMAIN_ERRORS if error not in HTTP_ERRORS]

    assert not missing, f"no HTTP mapping for {missing}"


def test_every_code_is_unique_snake_case() -> None:
    codes = [error.code for error in DOMAIN_ERRORS]

    assert len(set(codes)) == len(codes)
    assert all(re.fullmatch(r"[a-z]+(_[a-z]+)*", code) for code in codes)


def test_every_message_is_sentence_case_without_a_period() -> None:
    for error in DOMAIN_ERRORS:
        assert error.message[0].isupper(), error.__name__
        assert not error.message.endswith("."), error.__name__


def _imports_web_framework(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        if any(name.split(".")[0] in {"fastapi", "starlette"} for name in names):
            return True
    return False


def test_only_the_api_layer_imports_the_web_framework() -> None:
    api_layer = {path for path in SRC.rglob("*.py") if "api" in path.parts}
    api_layer |= set(SRC.rglob("router.py"))

    offenders = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if path not in api_layer and _imports_web_framework(path)
    ]

    assert not offenders

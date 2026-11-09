import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"


def test_no_legacy_query_calls() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "query"
            ):
                offenders.append(f"{path}:{node.lineno}")
    assert not offenders, f"legacy session.query() calls: {offenders}"

#!/usr/bin/env python3
"""Report the declared license of every package locked in `uv.lock`.

Run: uv run python scripts/license_audit.py

Reads the complete resolved tree, runtime and dev, on every platform the lock
covers, and looks up each exact version on PyPI. Exits 1 if any package has no
declared license, if a lookup fails, or if a copyleft license appears that is
not listed in REVIEWED_COPYLEFT.

What it does NOT do: this is not legal review. PyPI metadata is declared by
each author and nobody verifies it. It does not read LICENSE files, does not
inspect bundled third-party code, and says nothing about the system libraries
in the container image.

Traps, because getting these wrong is quiet:

* The license lives in three different PyPI fields (`license_expression`, the
  classifiers and the free-text `license`), and old releases often fill only
  one. An empty answer means "go read the LICENSE file", never "permissive".
* The free-text field sometimes holds the full license text, so it is cut short.
* Scope follows the lock's dependency edges, including extras. A copyleft
  package only used in development is not shipped in the image, so the two
  cases are reported apart.
"""

import concurrent.futures
import json
import re
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

LOCK_FILE = Path(__file__).resolve().parent.parent / "uv.lock"
PROJECT = "climate-alerts"
COPYLEFT = re.compile(
    r"\b(A?GPL|LGPL|MPL|EPL|EUPL|CDDL|OSL|CeCILL|SSPL|BUSL|CC-BY-SA)"
    r"|Commons Clause|GNU|Mozilla",
    re.I,
)
# package name -> where the justification lives. Add a package only after
# writing down why its license is acceptable.
REVIEWED_COPYLEFT: dict[str, str] = {
    "certifi": "docs/Decisiones.md, decision 2: MPL-2.0, dev only, not in the image",
    "pathspec": "docs/Decisiones.md, decision 2: MPL-2.0, dev only, not in the image",
}
UNKNOWN = "unknown"


def graph_from_lock(lock: dict) -> dict[str, dict]:
    return {package["name"]: package for package in lock["package"]}


def closure(packages: dict[str, dict], roots: list[dict]) -> set[str]:
    """Follow dependency edges from `roots`, entering the extras each edge names."""
    seen: set[tuple[str, str | None]] = set()
    pending = [(edge["name"], extra) for edge in roots for extra in _extras(edge)]
    while pending:
        name, extra = pending.pop()
        if (name, extra) in seen:
            continue
        seen.add((name, extra))
        package = packages[name]
        edges = (
            package.get("dependencies", [])
            if extra is None
            else package.get("optional-dependencies", {}).get(extra, [])
        )
        pending.extend((edge["name"], e) for edge in edges for e in _extras(edge))
    return {name for name, _ in seen}


def _extras(edge: dict) -> list[str | None]:
    """The base package always counts, and each named extra adds its own edges."""
    return [None, *edge.get("extra", [])]


def scopes(packages: dict[str, dict]) -> dict[str, str]:
    project = packages[PROJECT]
    runtime = closure(packages, project.get("dependencies", []))
    dev_roots = [e for g in project.get("dev-dependencies", {}).values() for e in g]
    everything = runtime | closure(packages, dev_roots)
    # Locked, but only reachable through an edge this walk treats as optional.
    unreached = set(packages) - everything - {PROJECT}
    if unreached:
        raise SystemExit(f"packages not reached from the project: {sorted(unreached)}")
    return {name: "runtime" if name in runtime else "dev" for name in everything}


def declared_license(name: str, version: str) -> str:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    request = urllib.request.Request(url, headers={"User-Agent": "license-audit/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            info = json.load(response)["info"]
    except (urllib.error.URLError, TimeoutError) as exc:
        return f"ERROR: {exc}"

    if info.get("license_expression"):
        return str(info["license_expression"])
    classifiers = [
        c.split("::")[-1].strip()
        for c in info.get("classifiers") or []
        if c.startswith("License ::")
    ]
    if classifiers:
        return " / ".join(classifiers)
    free_text = (info.get("license") or "").strip()
    return free_text[:60].replace("\n", " ") if free_text else UNKNOWN


def main() -> int:
    packages = graph_from_lock(tomllib.loads(LOCK_FILE.read_text()))
    scope_of = scopes(packages)
    names = sorted(scope_of)

    with concurrent.futures.ThreadPoolExecutor(12) as pool:
        found = pool.map(
            lambda name: declared_license(name, packages[name]["version"]), names
        )
        licenses = dict(zip(names, found, strict=True))

    for name in names:
        version = packages[name]["version"]
        print(f"{scope_of[name]:8} {name}=={version:14} {licenses[name]}")

    missing = [
        name
        for name in names
        if licenses[name] == UNKNOWN or licenses[name].startswith("ERROR")
    ]
    copyleft = [name for name in names if COPYLEFT.search(licenses[name])]
    unreviewed = [name for name in copyleft if name not in REVIEWED_COPYLEFT]
    runtime_count = sum(scope == "runtime" for scope in scope_of.values())

    print(
        f"\n{len(names)} packages ({runtime_count} runtime, "
        f"{len(names) - runtime_count} dev only), "
        f"{len(copyleft)} copyleft, {len(missing)} without a license"
    )
    for name in copyleft:
        print(f"copyleft ({scope_of[name]}): {name}: {licenses[name]}")
    for name in missing:
        print(f"NO LICENSE, read its LICENSE file: {name}: {licenses[name]}")
    for name in unreviewed:
        print(f"NOT REVIEWED, explain it and add it to REVIEWED_COPYLEFT: {name}")
    return 1 if missing or unreviewed else 0


if __name__ == "__main__":
    sys.exit(main())

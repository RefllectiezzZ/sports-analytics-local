"""Enforce that shared snapshot infrastructure stays sport-agnostic."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers import repository_root

FORBIDDEN_PREFIXES = (
    "sports_analytics.sports",
    "sports_analytics.markets",
    "sports_analytics.ingestion",
)


def _snapshot_modules() -> list[Path]:
    package = repository_root() / "src" / "sports_analytics" / "snapshots"
    modules = sorted(package.glob("*.py"))
    assert modules, "snapshot package modules were not found"
    return modules


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            imported.add(node.module)
    return imported


@pytest.mark.parametrize("module_path", _snapshot_modules(), ids=lambda path: path.name)
def test_snapshot_module_does_not_import_domain_packages(module_path: Path) -> None:
    imported = _imported_modules(module_path)

    offending = sorted(
        name
        for name in imported
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in FORBIDDEN_PREFIXES)
    )
    assert offending == [], (
        f"{module_path.name} imports sport/market/ingestion modules: {offending}"
    )


def test_importing_snapshots_does_not_load_sport_packages() -> None:
    script = (
        "import sports_analytics.snapshots as pkg\n"
        "import sports_analytics.snapshots.service\n"
        "import sports_analytics.snapshots.reader\n"
        "import sports_analytics.snapshots.writer\n"
        "import sports_analytics.snapshots.manifest\n"
        "import sports_analytics.snapshots.parquet\n"
        "import sys\n"
        "loaded = sorted(\n"
        "    name\n"
        "    for name in sys.modules\n"
        "    if name.startswith('sports_analytics.sports')\n"
        "    or name.startswith('sports_analytics.markets')\n"
        "    or name.startswith('sports_analytics.ingestion')\n"
        ")\n"
        "assert pkg is not None\n"
        "print(','.join(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository_root(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_generic_snapshot_public_api_exposes_no_sport_fields() -> None:
    from sports_analytics.snapshots.types import PublishedSnapshot

    field_names = set(PublishedSnapshot.__dataclass_fields__)

    forbidden = {
        "games_count",
        "teams_count",
        "odds_quotes_count",
        "statistics_rows_count",
        "competition_id",
        "season_id",
    }
    assert field_names & forbidden == set()
    assert {"partition_keys", "metrics", "domain_metadata"} <= field_names

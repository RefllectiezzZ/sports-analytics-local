"""Enforce that shared modelling infrastructure stays sport-agnostic."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest
from tests.helpers import repository_root

FORBIDDEN_PREFIXES = (
    "sports_analytics.features.football",
    "sports_analytics.models.football_1x2",
    "sports_analytics.sports.football",
)

SHARED_MODULE_PACKAGES = (
    "features/contracts.py",
    "models/contracts.py",
    "models/logistic.py",
    "models/calibration.py",
    "models/artifacts.py",
    "evaluation/metrics.py",
    "evaluation/temporal.py",
)


def _module_paths() -> list[Path]:
    root = repository_root() / "src" / "sports_analytics"
    return [root / relative for relative in SHARED_MODULE_PACKAGES]


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


@pytest.mark.parametrize("module_path", _module_paths(), ids=lambda path: path.name)
def test_shared_module_does_not_import_football_packages(module_path: Path) -> None:
    imported = _imported_modules(module_path)
    offending = sorted(
        name
        for name in imported
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in FORBIDDEN_PREFIXES)
    )
    assert offending == [], f"{module_path.name} imports football modules: {offending}"


def test_importing_shared_evaluation_does_not_load_football_packages() -> None:
    script = (
        "import sports_analytics.evaluation.metrics\n"
        "import sports_analytics.evaluation.temporal\n"
        "import sys\n"
        "loaded = sorted(\n"
        "    name\n"
        "    for name in sys.modules\n"
        "    if name.startswith('sports_analytics.features.football')\n"
        "    or name.startswith('sports_analytics.models.football_1x2')\n"
        ")\n"
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

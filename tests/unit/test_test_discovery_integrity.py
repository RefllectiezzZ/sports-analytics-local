"""Guard against silently uncollected test modules."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_every_test_module_contributes_to_full_collection() -> None:
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    collected = {
        line.split("::", maxsplit=1)[0].replace("\\", "/")
        for line in completed.stdout.splitlines()
        if line.startswith("tests/") or line.startswith("tests\\")
    }
    expected = {
        path.relative_to(root).as_posix()
        for path in (root / "tests").rglob("test_*.py")
        if "__pycache__" not in path.parts
    }
    assert expected == collected
